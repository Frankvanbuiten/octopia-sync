"""
Read the Channable custom export (CSV or JSON, by URL or local path) and map
each row to an Octopia `products-integration` product object.

Expected columns (rename in COLUMNS if the Channable export uses other names):
  ean, sku, category_code, brand, title_fr, description_fr, rich_description_fr,
  variant_group (optional), image, additional_image_link (and/or image_1 .. image_6),
  attr_<propertyReference> ...

Multi-valued attributes go in one cell separated by "|".
description_fr may contain HTML: it is converted to plain text and cut to 2000
characters. If it is empty, rich_description_fr is used as the source.
"""

from __future__ import annotations

import csv
import io
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import requests

COLUMNS = {
    "gtin": "ean",
    "sellerProductReference": "sku",
    "categoryCode": "category_code",
    "brand": "brand",
    "title": "title_fr",
    "description": "description_fr",
    "richMarketingDescription": "rich_description_fr",
    "variantGroupReference": "variant_group",
}
# Pictures are taken in this order, deduplicated and capped at Octopia's max of 6.
# Every cell may hold one URL or a list (JSON array, or separated by comma, | or spaces).
# URLs glued together without a separator (Channable's Combine) are split on the next "http".
IMAGE_COLUMNS = ["image", "additional_image_link"] + [f"image_{i}" for i in range(1, 7)]
MAX_PICTURES = 6
MAX_DESCRIPTION = 2000
URL_RE = re.compile(r"https?://.+?(?=https?://|[\s,|'\"\[\]]|$)")
# Octopia refuses pictures over 5 MB; Shopify's CDN resizes on request (never upscales).
SHOPIFY_IMAGE_WIDTH = 2000


def _sized(url: str) -> str:
    if "cdn.shopify.com" not in url or re.search(r"[?&]width=", url):
        return url
    return f"{url}{'&' if '?' in url else '?'}width={SHOPIFY_IMAGE_WIDTH}"
ATTR_PREFIX = "attr_"
MULTI_SEP = "|"


def load_rows(source: str, timeout: int = 120) -> list[dict]:
    if source.startswith(("http://", "https://")):
        r = requests.get(source, timeout=timeout)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig")
    else:
        text = Path(source).read_text(encoding="utf-8-sig")
    return parse_rows(text)


def parse_rows(text: str) -> list[dict]:
    stripped = text.lstrip()
    if stripped.startswith(("[", "{")):
        data = json.loads(stripped)
        if isinstance(data, dict):
            data = data.get("items") or data.get("products") or data.get("data") or []
        return [{str(k): v for k, v in row.items()} for row in data]
    # The header row decides the delimiter: csv.Sniffer gives up when rows have uneven lengths.
    header = text.split("\n", 1)[0]
    delimiter = max(",;\t", key=header.count)
    return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))


def _clean(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # JSON exports sometimes turn EANs into numbers
    return str(v).strip()


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str) -> str:
    """HTML → plain text: block tags become line breaks, list items get "- "."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = (re.sub(r"[ \t ]+", " ", l).strip() for l in "".join(parser.parts).splitlines())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    # Whitespace between <li> tags in the source would leave blank lines inside a list.
    return re.sub(r"(?m)^(- .*)\n\n(?=- )", r"\1\n", text).strip()


def truncate_text(text: str, limit: int = MAX_DESCRIPTION) -> str:
    """Cut at the last sentence end (or else the last space) that fits within limit."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"))
    if end >= limit * 0.6:
        return cut[:end + 1].rstrip()
    return cut[:cut.rfind(" ")].rstrip() + "…"


def row_to_product(row: dict) -> dict:
    """Map one export row to an Octopia product. Empty fields are left out."""
    product: dict = {}
    for key, col in COLUMNS.items():
        val = _clean(row.get(col))
        if val:
            product[key] = val

    # Octopia wants description as plain text ≤2000; derive it from the HTML when needed.
    desc = product.get("description") or product.get("richMarketingDescription", "")
    if desc:
        product["description"] = truncate_text(html_to_text(desc) if "<" in desc or "&" in desc else desc)

    urls: list[str] = []
    for col in IMAGE_COLUMNS:
        cell = row.get(col)
        if isinstance(cell, list):  # JSON export keeps arrays as arrays
            cell = " ".join(map(str, cell))
        for u in URL_RE.findall(_clean(cell)):
            if u not in urls:
                urls.append(u)
    urls = urls[:MAX_PICTURES]
    if urls:
        product["sellerPictureUrls"] = [{"index": i, "url": _sized(u)} for i, u in enumerate(urls, 1)]

    attrs = []
    for col in sorted(row):
        if not col.startswith(ATTR_PREFIX):
            continue
        raw = _clean(row[col])
        values = [v.strip() for v in raw.split(MULTI_SEP) if v.strip()]
        if values:
            attrs.append({"propertyReference": col[len(ATTR_PREFIX):], "values": values})
    if attrs:
        product["attributes"] = attrs
    return product


def load_products(source: str) -> list[dict]:
    """Rows → products, deduplicated on gtin (last row wins)."""
    by_gtin: dict[str, dict] = {}
    for row in load_rows(source):
        p = row_to_product(row)
        by_gtin[p.get("gtin", "")] = p
    return list(by_gtin.values())
