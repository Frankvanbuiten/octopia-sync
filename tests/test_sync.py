import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import octopia_client  # noqa: E402
import sync  # noqa: E402
from source_channable import html_to_text, parse_rows, row_to_product, truncate_text  # noqa: E402
from validate import CategoryInfo, PropertyRule, clean_attributes, validate_product  # noqa: E402

CSV = """ean;sku;category_code;brand;title_fr;description_fr;rich_description_fr;image_1;image_2;image_3;attr_11296;attr_2001
8720589000017;VOLDT-T2-7M;0R0401;Voldt;Câble de recharge Type 2 – 7 m;Câble robuste pour voiture électrique.;<p>Riche</p>;https://cdn.shopify.com/a.jpg;;https://cdn.shopify.com/c.jpg;22 kW;Noir | Gris
8720589000024;VOLDT-T2-5M;0R0401;Voldt;Câble 5 m;<b>HTML</b> interdit;;http://insecure/a.jpg;;;;
"""


def good_product(**over):
    p = {
        "gtin": "8720589000017", "sellerProductReference": "SKU", "categoryCode": "0R0401",
        "brand": "Voldt", "title": "Titre", "description": "Texte brut.",
        "sellerPictureUrls": [{"index": 1, "url": "https://x/a.jpg"}],
        "attributes": [{"propertyReference": "11296", "values": ["22 kW"]}],
    }
    p.update(over)
    return p


class MappingTest(unittest.TestCase):
    def test_csv_row_maps_to_payload(self):
        rows = parse_rows(CSV)
        p = row_to_product(rows[0])
        self.assertEqual(p["gtin"], "8720589000017")
        self.assertEqual(p["categoryCode"], "0R0401")
        self.assertEqual(p["richMarketingDescription"], "<p>Riche</p>")
        # empty image_2 is skipped and indexes are compacted
        self.assertEqual(p["sellerPictureUrls"], [
            {"index": 1, "url": "https://cdn.shopify.com/a.jpg?width=2000"},
            {"index": 2, "url": "https://cdn.shopify.com/c.jpg?width=2000"},
        ])
        self.assertEqual(p["attributes"], [
            {"propertyReference": "11296", "values": ["22 kW"]},
            {"propertyReference": "2001", "values": ["Noir", "Gris"]},
        ])
        self.assertNotIn("variantGroupReference", p)

    def test_image_plus_additional_image_link_formats(self):
        a, b, c = (f"https://cdn.shopify.com/{x}.jpg?v=17" for x in "abc")
        for additional in (f'["{b}", "{c}"]', f"['{b}', '{c}']", f"{b},{c}", f"{b} | {c}", [b, c]):
            p = row_to_product({"ean": "1", "image": a, "additional_image_link": additional})
            self.assertEqual([x["url"] for x in p["sellerPictureUrls"]], [u + "&width=2000" for u in (a, b, c)], additional)
            self.assertEqual([x["index"] for x in p["sellerPictureUrls"]], [1, 2, 3])

    def test_urls_glued_without_separator(self):
        base = "https://cdn.shopify.com/s/files/1/0753/8642/1582/files/voldt-danish-plug-"
        urls = [base + "main-image.png?v=1785960526"] + [
            f"{base}secondary-images-{i}.jpg?v=1785960524" for i in range(1, 7)]
        p = row_to_product({"ean": "1", "image": "".join(urls)})
        self.assertEqual([x["url"] for x in p["sellerPictureUrls"]], [u + "&width=2000" for u in urls[:6]])

    def test_pictures_deduplicated_and_capped_at_6(self):
        urls = [f"https://cdn.shopify.com/{i}.jpg" for i in range(9)]
        p = row_to_product({"image": urls[0], "additional_image_link": ",".join(urls)})
        self.assertEqual([x["url"] for x in p["sellerPictureUrls"]], [u + "?width=2000" for u in urls[:6]])

    def test_only_shopify_urls_without_width_are_resized(self):
        p = row_to_product({"image": "https://example.com/a.jpg https://cdn.shopify.com/b.jpg?v=1&width=800"})
        self.assertEqual([x["url"] for x in p["sellerPictureUrls"]],
                         ["https://example.com/a.jpg", "https://cdn.shopify.com/b.jpg?v=1&width=800"])

    def test_json_export_and_numeric_ean(self):
        rows = parse_rows(json.dumps([{"ean": 8720589000017.0, "title_fr": " x "}]))
        p = row_to_product(rows[0])
        self.assertEqual(p["gtin"], "8720589000017")
        self.assertEqual(p["title"], "x")
        self.assertNotIn("attributes", p)


class DescriptionTest(unittest.TestCase):
    def test_html_to_text(self):
        html = "<h2>Titre</h2>\n<p>Câble A &amp; B&nbsp;!</p><ul><li>7 m</li><li>22 kW</li></ul><p>Fin<br>ligne</p>"
        self.assertEqual(html_to_text(html), "Titre\n\nCâble A & B !\n\n- 7 m\n- 22 kW\n\nFin\nligne")

    def test_list_with_newlines_between_items(self):
        html = "<h2>Essentiel</h2>\n<ul>\n<li>Un</li>\n<li>Deux</li>\n<li>Trois</li>\n</ul>\n<p>Fin</p>"
        self.assertEqual(html_to_text(html), "Essentiel\n\n- Un\n- Deux\n- Trois\n\nFin")

    def test_truncate_on_sentence(self):
        text = ("Phrase numéro un. " * 150).strip()
        out = truncate_text(text)
        self.assertLessEqual(len(out), 2000)
        self.assertTrue(out.endswith("un."))

    def test_truncate_without_sentence_end(self):
        out = truncate_text("mot " * 600)
        self.assertLessEqual(len(out), 2000)
        self.assertTrue(out.endswith("mot…"))

    def test_description_from_rich_when_empty(self):
        p = row_to_product({"ean": "1", "rich_description_fr": "<p>Riche &eacute;t&eacute;</p>"})
        self.assertEqual(p["description"], "Riche été")
        self.assertEqual(p["richMarketingDescription"], "<p>Riche &eacute;t&eacute;</p>")

    def test_long_html_description_fits(self):
        p = row_to_product({"ean": "1", "description_fr": "<p>" + "Une phrase. " * 200 + "</p>"})
        self.assertLessEqual(len(p["description"]), 2000)
        self.assertEqual(validate_product({**good_product(), "description": p["description"]}), [])


class AttributeChoicesTest(unittest.TestCase):
    CAT = CategoryInfo(properties={
        "47478": PropertyRule("Connecteur", choices={"type 1": "Type 1", "type 2": "Type 2"}),
        "47479": PropertyRule("Fonctions", multiple=True, choices={"rfid": "RFID", "application mobile": "Application mobile"}),
        "999996": PropertyRule("Poids"),
    })

    def test_clean_attributes(self):
        p = good_product(attributes=[
            {"propertyReference": "47478", "values": ["type  2", "Type 1"]},
            {"propertyReference": "47479", "values": ["RFID", "Bluetooth"]},
            {"propertyReference": "999996", "values": ["1,2"]},
            {"propertyReference": "47477", "values": ["Up to 22kW"]},
        ])
        dropped = clean_attributes(p, self.CAT)
        self.assertEqual(p["attributes"], [
            {"propertyReference": "47478", "values": ["Type 2"]},
            {"propertyReference": "47479", "values": ["RFID"]},
            {"propertyReference": "999996", "values": ["1,2"]},
        ])
        self.assertEqual(sorted(dropped), [
            ("47477", "Up to 22kW", "not a property of this category"),
            ("47478", "Type 1", "only one value allowed"),
            ("47479", "Bluetooth", "not in choices"),
        ])

    def test_all_dropped_removes_key(self):
        p = good_product(attributes=[{"propertyReference": "47478", "values": ["CCS2 Femelle"]}])
        clean_attributes(p, self.CAT)
        self.assertNotIn("attributes", p)

    def test_no_property_info_keeps_everything(self):
        p = good_product()
        self.assertEqual(clean_attributes(p, CategoryInfo()), [])
        self.assertEqual(len(p["attributes"]), 1)


class ValidateTest(unittest.TestCase):
    def test_good_product_passes(self):
        self.assertEqual(validate_product(good_product()), [])

    def test_bad_row_from_csv(self):
        p = row_to_product(parse_rows(CSV)[1])
        errs = " | ".join(validate_product(p))
        self.assertNotIn("HTML", errs)  # converted to plain text by the mapping
        self.assertEqual(p["description"], "HTML interdit")
        self.assertIn("not HTTPS", errs)

    def test_validator_still_catches_html(self):
        self.assertTrue(any("HTML" in e for e in validate_product(good_product(description="<p>x</p>"))))

    def test_limits(self):
        errs = validate_product(good_product(gtin="123", title="t" * 133, description="d" * 2001,
                                             categoryCode="ABC", sellerPictureUrls=[]))
        self.assertEqual(len(errs), 5)

    def test_category_rules(self):
        cat = CategoryInfo(mandatory_properties={"11296", "999"}, is_brand_mandatory=True,
                           is_variant=True)
        errs = validate_product(good_product(brand=""), cat)
        self.assertTrue(any("brand" in e for e in errs))
        self.assertTrue(any("variantGroupReference" in e for e in errs))
        self.assertTrue(any("999" in e and "11296" not in e for e in errs))


class FakeResponse:
    def __init__(self, status, body, links=None):
        self.status_code = status
        self._body = body
        self.links = links or {}
        self.headers = {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class ClientTest(unittest.TestCase):
    def make(self, responses):
        session = mock.Mock()
        session.post.return_value = FakeResponse(200, {"access_token": "tok", "expires_in": 7200})
        session.request.side_effect = responses
        return octopia_client.OctopiaClient("id", "secret", "seller", session=session), session

    def test_headers_token_cache_and_pagination(self):
        client, session = self.make([
            FakeResponse(200, {"items": [{"gtin": "1"}]},
                         links={"next": {"url": "/products?cursor=abc&limit=100"}}),
            FakeResponse(200, {"items": [{"gtin": "2"}]}),
        ])
        self.assertEqual([p["gtin"] for p in client.iter_products()], ["1", "2"])
        self.assertEqual(session.post.call_count, 1)  # token cached
        headers = session.request.call_args.kwargs["headers"]
        self.assertEqual(headers["SellerId"], "seller")
        self.assertEqual(headers["Accept-Language"], "fr-FR")
        self.assertEqual(session.request.call_args.args[1],
                         octopia_client.BASE_URL + "/products?cursor=abc&limit=100")

    def test_reports_page_with_page_index(self):
        client, session = self.make([
            FakeResponse(200, {"items": [{"gtin": str(i)} for i in range(100)]}),
            FakeResponse(200, {"items": [{"gtin": "100"}]}),
        ])
        self.assertEqual(len(client.integration_reports("pkg")), 101)
        pages = [c.kwargs["params"]["pageIndex"] for c in session.request.call_args_list]
        self.assertEqual(pages, [1, 2])

    def test_401_refreshes_token_once(self):
        client, session = self.make([
            FakeResponse(401, {}),
            FakeResponse(202, {"packageId": "pkg-1"}),
        ])
        self.assertEqual(client.submit_products([{}]), "pkg-1")
        self.assertEqual(session.post.call_count, 2)

    def test_problem_json_error(self):
        client, _ = self.make([FakeResponse(400, {"title": "Bad", "traceId": "t1"})])
        with self.assertRaises(octopia_client.OctopiaError) as ctx:
            client.category("0R0401")
        self.assertIn("t1", str(ctx.exception))


class FakeClient:
    def __init__(self, status="Integrated", error_code="E1"):
        self.status = status
        self.error_code = error_code
        self.submitted = []

    def category(self, ref, lang="fr-FR"):
        label = "CABLE DE RECHARGE" if lang == "fr-FR" else "CHARGING CABLE"
        return {"label": label, "isBrandMandatory": True, "isVariant": False}

    def category_properties(self, ref, lang="fr-FR"):
        fr = lang == "fr-FR"
        return [{"propertyReference": "11296", "necessity": "Mandatory",
                 "label": "Puissance" if fr else "Power"},
                {"propertyReference": "2001", "necessity": "Optional", "label": "Couleur"}]

    def iter_products(self):
        return iter([{"gtin": "8720589000017", "permissions": {"edit": True, "enrich": True},
                      "category": {"reference": "0R0401", "label": "CABLE DE RECHARGE"}}])

    def submit_products(self, products):
        self.submitted.append(products)
        return f"pkg-{len(self.submitted)}"

    def integration_reports(self, package_id):
        return [{"gtin": p["gtin"], "status": self.status, "operationType": "Modification",
                 "errors": [{"code": self.error_code, "field": "title", "message": "nope"}]
                 if self.status == "Refused" else []}
                for p in self.submitted[int(package_id.split("-")[1]) - 1]]


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "export.csv"
        self.src.write_text(CSV, encoding="utf-8")
        self.state = self.tmp / "state.json"
        patches = [
            mock.patch.object(sync, "HERE", self.tmp),
            mock.patch.object(sync, "POLL_SCHEDULE", [0]),
            mock.patch.dict("os.environ", {"STATE_PATH": str(self.state)}),
        ]
        self.out = mock.patch("builtins.print").start()
        self.addCleanup(mock.patch.stopall)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_sync(self, client, *args):
        with mock.patch.object(sync, "make_client", return_value=client):
            return sync.main(["--source", str(self.src), *args])

    def test_send_then_diff_skips_unchanged(self):
        client = FakeClient()
        # row 2 is not on Octopia → skipped; row 1 is sent
        self.assertEqual(self.run_sync(client), 0)
        self.assertEqual([[p["gtin"] for p in b] for b in client.submitted], [["8720589000017"]])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["8720589000017"]["status"], "Integrated")
        self.assertEqual(state["8720589000017"]["operationType"], "Modification")

        self.run_sync(client)
        self.assertEqual(len(client.submitted), 1)  # unchanged → not resent

        self.src.write_text(CSV.replace("7 m", "7 mètres"), encoding="utf-8")
        self.run_sync(client)
        self.assertEqual(len(client.submitted), 2)  # changed → resent

        self.assertEqual(self.run_sync(client, "--ean", "8720589000017"), 0)
        self.assertEqual(len(client.submitted), 3)  # --ean forces

    def test_quota_refusal_is_resent_next_run(self):
        client = FakeClient("Refused", error_code="QuotaExceeded")
        self.assertEqual(self.run_sync(client), 0)  # quota is retried, not a failure
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["8720589000017"]["status"], "QuotaExceeded")
        client.status = "Integrated"
        self.assertEqual(self.run_sync(client), 0)  # no --ean/--all: the diff resends it
        self.assertEqual(len(client.submitted), 2)

    def test_content_refusal_is_not_resent_unchanged(self):
        client = FakeClient("Refused")
        self.run_sync(client)
        self.run_sync(client)
        self.assertEqual(len(client.submitted), 1)

    def test_pending_is_picked_up_by_status(self):
        client = FakeClient()
        with mock.patch.object(sync, "POLL_MAX_SECONDS", -1):  # give up before the first poll
            self.assertEqual(self.run_sync(client), 0)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["8720589000017"]["status"], "Pending")
        self.assertEqual(self.run_sync(client, "--status"), 0)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["8720589000017"]["status"], "Integrated")
        self.assertEqual(len(client.submitted), 1)  # --status never sends

    def test_refused_gives_exit_1_and_logs(self):
        self.assertEqual(self.run_sync(FakeClient("Refused"), "--ean", "8720589000017"), 1)
        lines = [json.loads(l) for f in (self.tmp / "logs").glob("*.jsonl")
                 for l in f.read_text(encoding="utf-8").splitlines()]
        refused = [l for l in lines if l["status"] == "Refused"]
        self.assertEqual(refused[0]["errors"], ["E1 title: nope"])

    def test_dry_run_sends_nothing_and_needs_no_credentials(self):
        self.assertEqual(self.run_sync(None, "--dry-run", "--ean", "8720589000017"), 0)
        self.assertFalse(self.state.exists())

    def test_missing_mandatory_attribute_blocks_send(self):
        self.src.write_text(CSV.replace(";22 kW;", ";;"), encoding="utf-8")
        client = FakeClient()
        self.assertEqual(self.run_sync(client, "--ean", "8720589000017"), 1)
        self.assertEqual(client.submitted, [])

    def printed(self):
        return "\n".join(" ".join(map(str, c.args)) for c in self.out.call_args_list)

    def test_variant_group_only_sent_for_variant_category(self):
        self.src.write_text(CSV.replace("attr_2001\n", "attr_2001;variant_group\n")
                               .replace("Noir | Gris\n", "Noir | Gris;GRP1\n"), encoding="utf-8")
        for is_variant, expected in ((False, None), (True, "GRP1")):
            client = FakeClient()
            client.category = lambda ref, lang="fr-FR", v=is_variant: {"isVariant": v}
            self.run_sync(client, "--ean", "8720589000017")
            self.assertEqual(client.submitted[0][0].get("variantGroupReference"), expected)

    def test_empty_category_is_filled_from_octopia(self):
        self.src.write_text(CSV.replace(";0R0401;", ";;"), encoding="utf-8")
        client = FakeClient()
        self.assertEqual(self.run_sync(client, "--ean", "8720589000017"), 0)
        self.assertEqual(client.submitted[0][0]["categoryCode"], "0R0401")

    def test_product_not_on_octopia_is_skipped_not_invalid(self):
        self.src.write_text(CSV.replace("8720589000024", "8720589000031"), encoding="utf-8")
        client = FakeClient()
        client.iter_products = lambda: iter([])
        self.assertEqual(self.run_sync(client), 0)
        self.assertEqual(client.submitted, [])
        self.assertIn("not on Octopia, skipped", self.printed())

    def test_overview_shows_category_and_mandatory_attributes(self):
        self.src.write_text(CSV.replace(";22 kW;", ";;"), encoding="utf-8")
        self.assertEqual(self.run_sync(FakeClient(), "--check-permissions"), 0)
        out = self.printed()
        self.assertIn("0R0401  CABLE DE RECHARGE  /  EN: CHARGING CABLE", out)
        self.assertIn("attr_11296", out)
        self.assertIn("Puissance  /  EN: Power  [missing for 1/1]", out)
        self.assertIn("8720589000024  not on Octopia", out)

    def test_list_attributes_writes_csv_without_export(self):
        client = FakeClient()
        props = client.category_properties
        client.category_properties = lambda ref, lang="fr-FR": [
            {**x, "choices": ["Noir", "Gris"]} if x["propertyReference"] == "2001" else x
            for x in props(ref, lang)]
        with mock.patch.dict("os.environ", {"CHANNABLE_EXPORT_URL": ""}):
            self.assertEqual(self.run_sync(client, "--list-attributes"), 0)
        import csv
        with (self.tmp / "attributes.csv").open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f, delimiter=";"))
        self.assertEqual([r["column"] for r in rows], ["attr_11296", "attr_2001"])
        self.assertEqual(rows[0]["necessity"], "Mandatory")
        self.assertEqual(rows[0]["label_en"], "Power")
        self.assertEqual(rows[0]["category_en"], "CHARGING CABLE")
        self.assertEqual(rows[1]["choices_fr"], "Noir | Gris")


if __name__ == "__main__":
    unittest.main()
