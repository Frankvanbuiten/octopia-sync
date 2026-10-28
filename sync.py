#!/usr/bin/env python3
"""
Push Cdiscount product content (titles, descriptions, attributes, pictures) for
existing listings to Octopia via POST /products-integration.

Channable keeps handling price, stock and shipping; this only sends content.
Only products whose payload changed since the last run are sent, unless --all
or --ean is given.

Usage:
  py sync.py --dry-run                 # show what would be sent, send nothing
  py sync.py --ean 8720xxxxxxxxx       # send one product (repeatable)
  py sync.py                           # send all changed products
  py sync.py --all                     # resend everything
  py sync.py --check-permissions       # rights, category and mandatory attributes, send nothing
  py sync.py --list-attributes         # all attributes of Voldt's categories → attributes.csv

Exit codes: 0 ok (QuotaExceeded products are retried next run), 1 refused or locally
invalid products, 2 fatal error.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from octopia_client import OctopiaClient, OctopiaError
from source_channable import load_products
from validate import CategoryInfo, PropertyRule, clean_attributes, validate_product

HERE = Path(__file__).resolve().parent
FINAL_STATUSES = {"Refused", "Integrated"}
POLL_SCHEDULE = [5, 15, 30, 60]  # then every 60 s
POLL_MAX_SECONDS = 600
BATCH_PAUSE = 30  # Octopia has an undocumented submission quota (QuotaExceeded)


def load_env(path: Path) -> None:
    """Minimal .env reader; real environment variables take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def payload_hash(product: dict) -> str:
    blob = json.dumps(product, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


class RunLog:
    def __init__(self, log_dir: Path):
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
        self._f = self.path.open("a", encoding="utf-8")

    def write(self, ean: str, status: str, operation_type: str = "", package_id: str = "",
              errors: list | None = None, warnings: list | None = None) -> None:
        self._f.write(json.dumps({
            "timestamp": now_iso(), "ean": ean, "status": status,
            "operationType": operation_type, "packageId": package_id,
            "errors": errors or [], "warnings": warnings or [],
        }, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def make_client() -> OctopiaClient | None:
    cid, secret, seller = (os.environ.get(k) for k in
                           ("OCTOPIA_CLIENT_ID", "OCTOPIA_CLIENT_SECRET", "OCTOPIA_SELLER_ID"))
    if cid and secret and seller:
        return OctopiaClient(cid, secret, seller)
    return None


def fetch_category_infos(client: OctopiaClient, codes: set[str]) -> dict[str, CategoryInfo]:
    infos = {}
    for code in sorted(codes):
        try:
            cat = client.category(code)
            props = client.category_properties(code)
        except OctopiaError as e:
            print(f"  ! category {code}: {e}", file=sys.stderr)
            continue
        infos[code] = CategoryInfo(
            mandatory_properties={p["propertyReference"] for p in props
                                  if p.get("necessity") == "Mandatory"},
            is_brand_mandatory=bool(cat.get("isBrandMandatory")),
            is_variant=bool(cat.get("isVariant")),
            properties={p["propertyReference"]: PropertyRule(
                label=p.get("label", ""),
                multiple=bool(p.get("isMultiple")),
                choices={" ".join(c.split()).lower(): c for c in p.get("choices") or []},
            ) for p in props},
        )
    return infos


def print_dropped(dropped: list[tuple[str, str, str, str]], categories: dict[str, CategoryInfo]) -> None:
    """Summary of attribute values left out, per attribute: the values to fix in Channable."""
    if not dropped:
        return
    by_ref: dict[str, Counter] = {}
    for _, ref, value, reason in dropped:
        by_ref.setdefault(ref, Counter())[(value, reason)] += 1
    print(f"\n{len(dropped)} attribute values left out (product is still sent without them):")
    for ref, counts in sorted(by_ref.items()):
        label = next((c.properties[ref].label for c in categories.values() if ref in c.properties), "")
        print(f"  attr_{ref} {label} — {sum(counts.values())}×")
        for (value, reason), n in counts.most_common(8):
            print(f"      {n:>4}× {value!r}: {reason}")
        if len(counts) > 8:
            print(f"      … and {len(counts) - 8} other values")
    print()


def fetch_existing(client: OctopiaClient) -> dict[str, dict]:
    """gtin -> {permissions, category} for every product sheet on the seller account."""
    return {p["gtin"]: {"permissions": p.get("permissions") or {},
                        "category": p.get("category") or {}}
            for p in client.iter_products() if p.get("gtin")}


def fill_categories(products: list[dict], existing: dict[str, dict]) -> None:
    """Products without a category_code in the export keep their current Octopia category."""
    for p in products:
        current = existing.get(p.get("gtin", ""), {}).get("category", {}).get("reference")
        if not p.get("categoryCode") and current:
            p["categoryCode"] = current
        elif current and p.get("categoryCode") != current:
            print(f"  ! {p.get('gtin')}: export category {p['categoryCode']} differs from "
                  f"current Octopia category {current}")


def _try(fn, default):
    try:
        return fn()
    except OctopiaError:
        return default


NECESSITY_ORDER = {"Mandatory": 0, "Recommended": 1, "Optional": 2}
ATTRIBUTE_COLUMNS = ["category_code", "category_fr", "category_en", "products", "column",
                     "label_fr", "label_en", "necessity", "multiple_values", "unit",
                     "choices_fr", "allowed_file_types"]


def write_attribute_list(client: OctopiaClient, existing: dict[str, dict], path: Path) -> int:
    """Every property of every category Voldt has products in, as a CSV to fill Channable from."""
    counts: dict[str, int] = {}
    for info in existing.values():
        ref = info["category"].get("reference")
        if ref:
            counts[ref] = counts.get(ref, 0) + 1
    rows = []
    for code in sorted(counts):
        fr = _try(lambda: client.category(code), {})
        en = _try(lambda: client.category(code, lang="en-US"), {})
        props = _try(lambda: client.category_properties(code), [])
        labels_en = {x["propertyReference"]: x.get("label", "")
                     for x in _try(lambda: client.category_properties(code, lang="en-US"), [])}
        props.sort(key=lambda x: (NECESSITY_ORDER.get(x.get("necessity"), 3), x.get("index") or 0))
        print(f"  {code}  {fr.get('label', '?')} / {en.get('label', '?')}: "
              f"{len(props)} attributes, {counts[code]} products")
        for x in props:
            ref = x["propertyReference"]
            rows.append({
                "category_code": code, "category_fr": fr.get("label", ""),
                "category_en": en.get("label", ""), "products": counts[code],
                "column": f"attr_{ref}", "label_fr": x.get("label", ""),
                "label_en": labels_en.get(ref, ""), "necessity": x.get("necessity", ""),
                "multiple_values": "yes" if x.get("isMultiple") else "",
                "unit": x.get("unit") or "",
                "choices_fr": " | ".join(x.get("choices") or []),
                "allowed_file_types": ", ".join(x.get("allowedFileTypes") or []),
            })
    # utf-8-sig and ; so Excel opens it correctly with French accents
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ATTRIBUTE_COLUMNS, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} attributes in {len(counts)} categories → {path}")
    return 0


def print_overview(client: OctopiaClient, products: list[dict], existing: dict[str, dict]) -> int:
    """Per EAN: rights and current category; per category: FR/EN label and mandatory attributes."""
    bad = 0
    by_cat: dict[str, list[dict]] = {}
    print("\nProducts")
    for p in products:
        g = p.get("gtin", "")
        info = existing.get(g)
        if info is None:
            print(f"  {g}  not on Octopia")
            continue
        perm, cat = info["permissions"], info["category"]
        bad += not perm.get("edit")
        code = p.get("categoryCode") or cat.get("reference", "?")
        by_cat.setdefault(code, []).append(p)
        print(f"  {g}  edit={perm.get('edit')}  enrich={perm.get('enrich')}  category={code}")

    print("\nCategories")
    for code, prods in sorted(by_cat.items()):
        fr = _try(lambda: client.category(code), {})
        en = _try(lambda: client.category(code, lang="en-US"), {})
        print(f"\n  {code}  {fr.get('label', '?')}  /  EN: {en.get('label', '?')}"
              f"  ({len(prods)} products, brand mandatory={fr.get('isBrandMandatory')},"
              f" variant={fr.get('isVariant')})")
        props_fr = _try(lambda: client.category_properties(code), [])
        props_en = {x["propertyReference"]: x.get("label", "")
                    for x in _try(lambda: client.category_properties(code, lang="en-US"), [])}
        mandatory = [x for x in props_fr if x.get("necessity") == "Mandatory"]
        if not mandatory:
            print("    no mandatory attributes")
        for x in mandatory:
            ref = x["propertyReference"]
            missing = sum(1 for p in prods
                          if ref not in {a["propertyReference"] for a in p.get("attributes", [])})
            status = "ok" if not missing else f"missing for {missing}/{len(prods)}"
            print(f"    attr_{ref:<8} {x.get('label', '')}  /  EN: {props_en.get(ref, '?')}  [{status}]")

    print(f"\n{bad} product(s) without edit permission")
    return 1 if bad else 0


def summarize_issues(issues: list[dict] | None) -> list[str]:
    return [f"{i.get('code', '')} {i.get('field', '')}: {i.get('message', '')}".strip()
            for i in issues or []]


def poll_reports(client: OctopiaClient, packages: dict[str, list[str]]) -> dict[str, dict]:
    """Poll every package until all its GTINs have a final status, or timeout.
    Returns gtin -> latest report item."""
    results: dict[str, dict] = {}
    pending = dict(packages)
    started = time.time()
    step = 0
    while pending:
        wait = POLL_SCHEDULE[min(step, len(POLL_SCHEDULE) - 1)]
        if time.time() - started + wait > POLL_MAX_SECONDS:
            break
        time.sleep(wait)
        step += 1
        for package_id, gtins in list(pending.items()):
            try:
                items = client.integration_reports(package_id)
            except OctopiaError as e:
                if e.status == 404:  # report not available yet
                    continue
                raise
            for item in items:
                results[item.get("gtin", "")] = {**item, "packageId": package_id}
            if all(results.get(g, {}).get("status") in FINAL_STATUSES for g in gtins):
                del pending[package_id]
        done = sum(1 for g in results if results[g].get("status") in FINAL_STATUSES)
        total = sum(len(g) for g in packages.values())
        print(f"  … {done}/{total} products have a final status")
    return results


QUOTA = "QuotaExceeded"
OPEN_STATUSES = {"Submitted", "Pending"}


def apply_reports(state: dict, packages: dict[str, list[str]], results: dict[str, dict],
                  log: RunLog) -> Counter:
    """Write report outcomes to state and log; print refusals and a summary per status.

    A product refused for content keeps its hash, so it is resent once its source row
    changes. A product refused only because of Octopia's submission quota gets its hash
    cleared, so the next run resends it unchanged.
    """
    counts: Counter = Counter()
    for package_id, gtins in packages.items():
        for g in gtins:
            rep = results.get(g)
            status = rep.get("status", "Pending") if rep else "Pending"
            op = rep.get("operationType", "") if rep else ""
            errors = (rep or {}).get("errors") or []
            errs = summarize_issues(errors)
            warns = summarize_issues((rep or {}).get("warnings"))
            entry = state.setdefault(g, {"hash": "", "packageId": package_id})
            if status == "Refused" and errors and all(e.get("code") == QUOTA for e in errors):
                status = QUOTA
                entry["hash"] = ""
            entry.update(status=status, operationType=op, updatedAt=now_iso())
            counts[status] += 1
            log.write(g, status, op, package_id, errs, warns)
            if status == "Refused":
                print(f"  ✗ {g}: Refused — {'; '.join(errs)}")
    summary = ", ".join(f"{n} {s}" for s, n in counts.most_common())
    print(f"Result: {summary}")
    if counts[QUOTA]:
        print(f"  {counts[QUOTA]} hit Octopia's submission quota; the next run resends them.")
    if counts["Pending"]:
        print(f"  {counts['Pending']} still being processed; check later with --status.")
    return counts


def refresh_open(client: OctopiaClient, state: dict, log: RunLog) -> Counter:
    """Fetch reports once for products still Submitted/Pending from earlier runs."""
    packages: dict[str, list[str]] = {}
    for g, entry in state.items():
        if entry.get("status") in OPEN_STATUSES and entry.get("packageId"):
            packages.setdefault(entry["packageId"], []).append(g)
    if not packages:
        return Counter()
    print(f"Checking {sum(map(len, packages.values()))} products still open from earlier runs")
    results: dict[str, dict] = {}
    for package_id in packages:
        for item in client.integration_reports(package_id):
            results[item.get("gtin", "")] = item
    return apply_reports(state, packages, results, log)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show payloads, send nothing")
    ap.add_argument("--all", action="store_true", help="send every product, ignoring the diff")
    ap.add_argument("--ean", action="append", default=[], help="only this GTIN (repeatable)")
    ap.add_argument("--source", help="export URL or local file (default: CHANNABLE_EXPORT_URL)")
    ap.add_argument("--check-permissions", action="store_true",
                    help="show rights, current category and mandatory attributes per EAN, then exit")
    ap.add_argument("--no-wait", action="store_true", help="submit without polling reports")
    ap.add_argument("--list-attributes", action="store_true",
                    help="write every attribute of Voldt's categories to attributes.csv and exit "
                         "(needs no Channable export)")
    ap.add_argument("--status", action="store_true",
                    help="fetch reports for products still open from earlier runs, then exit")
    args = ap.parse_args(argv)

    load_env(HERE / ".env")
    batch_size = int(os.environ.get("BATCH_SIZE", "50"))
    state_path = Path(os.environ.get("STATE_PATH", HERE / "state.json"))

    client = make_client()
    if client is None and not args.dry_run:
        print("Missing OCTOPIA_CLIENT_ID / OCTOPIA_CLIENT_SECRET / OCTOPIA_SELLER_ID", file=sys.stderr)
        return 2

    if args.list_attributes:
        print("Fetching current products from Octopia")
        return write_attribute_list(client, fetch_existing(client), HERE / "attributes.csv")

    if args.status:
        state = load_state(state_path)
        log = RunLog(HERE / "logs")
        refresh_open(client, state, log)
        save_state(state_path, state)
        log.close()
        totals = Counter(e.get("status") for e in state.values())
        print("All products in state: " + ", ".join(f"{n} {s}" for s, n in totals.most_common()))
        return 1 if totals["Refused"] else 0

    source = args.source or os.environ.get("CHANNABLE_EXPORT_URL")
    if not source:
        print("No source: set CHANNABLE_EXPORT_URL or pass --source", file=sys.stderr)
        return 2

    print(f"Loading export: {source}")
    products = load_products(source)
    if args.ean:
        wanted = set(args.ean)
        products = [p for p in products if p.get("gtin") in wanted]
        missing = wanted - {p.get("gtin") for p in products}
        if missing:
            print(f"  ! not in export: {', '.join(sorted(missing))}", file=sys.stderr)
    print(f"  {len(products)} products in scope")

    existing: dict[str, dict] = {}
    if client:
        print("Fetching current products from Octopia")
        existing = fetch_existing(client)
        fill_categories(products, existing)

    if args.check_permissions:
        return print_overview(client, products, existing)

    # ── validate ────────────────────────────────────────────────────────────
    categories: dict[str, CategoryInfo] = {}
    if client:
        codes = {p["categoryCode"] for p in products if len(p.get("categoryCode", "")) == 6}
        print(f"Fetching rules for {len(codes)} categories")
        categories = fetch_category_infos(client, codes)
    else:
        print("  (no Octopia credentials: skipping category checks)")

    state = load_state(state_path)
    log = None if args.dry_run else RunLog(HERE / "logs")
    if log:
        refresh_open(client, state, log)
        save_state(state_path, state)
    valid, invalid = [], 0
    dropped: list[tuple[str, str, str, str]] = []
    for p in products:
        # v1 only updates sheets that already exist; creation is out of scope.
        if client and p.get("gtin") not in existing:
            print(f"  - {p.get('gtin', '?')}: not on Octopia, skipped")
            if log:
                log.write(p.get("gtin", ""), "SkippedNotOnOctopia")
            continue
        category = categories.get(p.get("categoryCode", ""))
        if category:
            # Octopia warns (VariantGroupReferenceNotAllowed) when a non-variant category gets one.
            if not category.is_variant:
                p.pop("variantGroupReference", None)
            gone = clean_attributes(p, category)
            dropped += [(p["gtin"], *d) for d in gone]
            if gone and log:
                log.write(p["gtin"], "AttributesDropped",
                          warnings=[f"attr_{r} {v!r}: {why}" for r, v, why in gone])
        errs = validate_product(p, category)
        if errs:
            invalid += 1
            print(f"  ✗ {p.get('gtin', '?')}: " + "; ".join(errs))
            if log:
                log.write(p.get("gtin", ""), "LocalInvalid", errors=errs)
        else:
            valid.append(p)

    print_dropped(dropped, categories)

    # ── diff ────────────────────────────────────────────────────────────────
    force = args.all or bool(args.ean)
    to_send = [p for p in valid
               if force or state.get(p["gtin"], {}).get("hash") != payload_hash(p)]
    print(f"{len(valid)} valid, {invalid} invalid, {len(to_send)} to send")

    if args.dry_run:
        print(json.dumps({"products": to_send}, indent=2, ensure_ascii=False))
        return 1 if invalid else 0
    if not to_send:
        log.close()
        return 1 if invalid else 0

    sendable = []
    for p in to_send:
        if not existing[p["gtin"]]["permissions"].get("edit"):
            print(f"  ✗ {p['gtin']}: no edit permission, skipped")
            log.write(p["gtin"], "SkippedNoEditPermission")
            invalid += 1
        else:
            sendable.append(p)

    # ── submit ──────────────────────────────────────────────────────────────
    packages: dict[str, list[str]] = {}
    for i in range(0, len(sendable), batch_size):
        batch = sendable[i:i + batch_size]
        if i:
            time.sleep(BATCH_PAUSE)
        package_id = client.submit_products(batch)
        packages[package_id] = [p["gtin"] for p in batch]
        print(f"  submitted {len(batch)} products → package {package_id}")
        for p in batch:
            state[p["gtin"]] = {"hash": payload_hash(p), "status": "Submitted",
                                "operationType": "", "packageId": package_id,
                                "updatedAt": now_iso()}
            log.write(p["gtin"], "Submitted", package_id=package_id)
        save_state(state_path, state)

    if args.no_wait or not packages:
        print(f"Log: {log.path}")
        log.close()
        return 1 if invalid else 0

    # ── reports ─────────────────────────────────────────────────────────────
    print("Waiting for integration reports")
    counts = apply_reports(state, packages, poll_reports(client, packages), log)
    save_state(state_path, state)
    print(f"Log: {log.path}")
    log.close()
    # Quota refusals resend themselves on the next run, so they don't fail a scheduled job.
    return 1 if counts["Refused"] or invalid else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main())
    except OctopiaError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(2)
