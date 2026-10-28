"""
Local validation of an Octopia product object before it is sent.

Limits follow the products-integration schema: title ≤132, description ≤2000 and
plain text, richMarketingDescription ≤5000, 1–6 HTTPS pictures, 6-char category.
Category rules (mandatory properties, brand, variant group) are checked when a
CategoryInfo is supplied; without one only the static checks run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

GTIN_RE = re.compile(r"^\d{8,13}$")
HTML_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>|&(?:[a-zA-Z]+|#\d+);")


@dataclass
class PropertyRule:
    label: str = ""
    multiple: bool = False
    choices: dict[str, str] = field(default_factory=dict)  # lower-cased → official spelling


@dataclass
class CategoryInfo:
    mandatory_properties: set[str] = field(default_factory=set)
    is_brand_mandatory: bool = False
    is_variant: bool = False
    properties: dict[str, PropertyRule] = field(default_factory=dict)


def clean_attributes(p: dict, category: CategoryInfo) -> list[tuple[str, str, str]]:
    """Drop attribute values Octopia would refuse, in place.

    The export uses one set of attr_ columns for every category, so a property the
    category doesn't have is dropped too. Values that match a choice apart from
    case/spacing are rewritten to the official spelling. Returns the dropped
    values as (propertyReference, value, reason).
    """
    if not category.properties:
        return []
    dropped: list[tuple[str, str, str]] = []
    kept = []
    for attr in p.get("attributes", []):
        ref = attr["propertyReference"]
        rule = category.properties.get(ref)
        if rule is None:
            dropped += [(ref, v, "not a property of this category") for v in attr["values"]]
            continue
        values = []
        for v in attr["values"]:
            if rule.choices:
                official = rule.choices.get(" ".join(v.split()).lower())
                if official is None:
                    dropped.append((ref, v, "not in choices"))
                    continue
                v = official
            values.append(v)
        if len(values) > 1 and not rule.multiple:
            dropped += [(ref, v, "only one value allowed") for v in values[1:]]
            values = values[:1]
        if values:
            kept.append({"propertyReference": ref, "values": values})
    if kept:
        p["attributes"] = kept
    else:
        p.pop("attributes", None)
    return dropped


def validate_product(p: dict, category: CategoryInfo | None = None) -> list[str]:
    errors: list[str] = []

    gtin = p.get("gtin", "")
    if not GTIN_RE.match(gtin):
        errors.append(f"gtin '{gtin}' must be 8-13 digits")

    title = p.get("title", "")
    if not title:
        errors.append("title is empty")
    elif len(title) > 132:
        errors.append(f"title is {len(title)} chars (max 132)")

    desc = p.get("description", "")
    if not desc:
        errors.append("description is empty")
    elif len(desc) > 2000:
        errors.append(f"description is {len(desc)} chars (max 2000)")
    if desc and HTML_RE.search(desc):
        errors.append("description contains HTML (plain text only)")

    rich = p.get("richMarketingDescription", "")
    if len(rich) > 5000:
        errors.append(f"richMarketingDescription is {len(rich)} chars (max 5000)")

    cat = p.get("categoryCode", "")
    if len(cat) != 6:
        errors.append(f"categoryCode '{cat}' must be 6 characters")

    pics = p.get("sellerPictureUrls", [])
    if not 1 <= len(pics) <= 6:
        errors.append(f"{len(pics)} pictures (need 1-6)")
    for pic in pics:
        if not pic["url"].lower().startswith("https://"):
            errors.append(f"picture {pic['index']} is not HTTPS: {pic['url']}")

    if category:
        if category.is_brand_mandatory and not p.get("brand"):
            errors.append("brand is mandatory for this category")
        if category.is_variant and not p.get("variantGroupReference"):
            errors.append("variantGroupReference is mandatory for this category")
        present = {a["propertyReference"] for a in p.get("attributes", [])}
        missing = sorted(category.mandatory_properties - present)
        if missing:
            errors.append("missing mandatory attributes: " + ", ".join(missing))

    return errors
