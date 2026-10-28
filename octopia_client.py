"""
Thin client for the Octopia seller API (v2), limited to what the content sync needs.

Auth is OAuth2 client_credentials. The token lives ~7200 s and the token endpoint
is capped at 500 requests/hour, so we cache it in-memory and only re-request
after ~110 minutes or on a 401.

Pagination on list endpoints is cursor-based; the next page URL is delivered in
the `Link` response header (rel="next"), not in the body.
"""

from __future__ import annotations

import time
from typing import Iterator

import requests

TOKEN_URL = "https://auth.octopia-io.net/auth/realms/maas/protocol/openid-connect/token"
BASE_URL = "https://api.octopia-io.net/seller/v2"
TOKEN_TTL = 110 * 60  # refresh well before the ~7200 s expiry


class OctopiaError(RuntimeError):
    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        if isinstance(body, dict):
            detail = body.get("title") or body.get("detail") or ""
            errs = body.get("errors")
            if errs:
                detail = f"{detail} {errs}"
            trace = body.get("traceId")
            if trace:
                detail = f"{detail} (traceId {trace})"
        else:
            detail = str(body)[:500]
        super().__init__(f"Octopia HTTP {status}: {detail}".strip())


class OctopiaClient:
    def __init__(self, client_id: str, client_secret: str, seller_id: str,
                 session: requests.Session | None = None, timeout: int = 60):
        self.client_id = client_id
        self.client_secret = client_secret
        self.seller_id = seller_id
        self.http = session or requests.Session()
        self.timeout = timeout
        self._token: str | None = None
        self._token_at = 0.0

    # ── auth ────────────────────────────────────────────────────────────────
    def _get_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() - self._token_at < TOKEN_TTL:
            return self._token
        r = self.http.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }, timeout=self.timeout)
        if r.status_code != 200:
            raise OctopiaError(r.status_code, _body(r))
        self._token = r.json()["access_token"]
        self._token_at = time.time()
        return self._token

    def _headers(self, lang: str) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "SellerId": self.seller_id,
            "Accept-Language": lang,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path_or_url: str, lang: str = "fr-FR", **kw) -> requests.Response:
        url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
        for attempt in range(4):
            r = self.http.request(method, url, headers=self._headers(lang), timeout=self.timeout, **kw)
            if r.status_code == 401 and attempt == 0:
                self._get_token(force=True)
                continue
            if r.status_code in (429, 502, 503, 504) and attempt < 3:
                wait = int(r.headers.get("Retry-After", 0) or 0) or 5 * (attempt + 1)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise OctopiaError(r.status_code, _body(r))
            return r
        raise OctopiaError(r.status_code, _body(r))

    def _paginate(self, path: str, params: dict) -> Iterator[dict]:
        r = self._request("GET", path, params=params)
        while True:
            for item in r.json().get("items") or []:
                yield item
            nxt = r.links.get("next", {}).get("url")
            if not nxt:
                return
            if not nxt.startswith("http"):
                nxt = BASE_URL + nxt
            r = self._request("GET", nxt)

    # ── endpoints ───────────────────────────────────────────────────────────
    def submit_products(self, products: list[dict]) -> str:
        """POST /products-integration. Returns the packageId (HTTP 202)."""
        r = self._request("POST", "/products-integration", json={"products": products})
        body = _body(r)
        package_id = body.get("packageId") if isinstance(body, dict) else None
        if not package_id:
            raise OctopiaError(r.status_code, f"no packageId in response: {body}")
        return package_id

    def integration_reports(self, package_id: str) -> list[dict]:
        # Unlike /products this endpoint ignores cursor/limit and sends no Link header:
        # it pages with pageIndex/pageSize (max 100, default 25).
        items: list[dict] = []
        page = 1
        while True:
            body = self._request("GET", "/products-integration-reports", params={
                "packageId": package_id, "pageIndex": page, "pageSize": 100}).json()
            batch = body.get("items") or []
            items += batch
            if len(batch) < 100:
                return items
            page += 1

    def iter_products(self) -> Iterator[dict]:
        return self._paginate("/products", {"limit": 100})

    # Category endpoints also answer in English (docs: EN, FR, ES); content itself must be fr-FR.
    def category(self, ref: str, lang: str = "fr-FR") -> dict:
        return self._request("GET", f"/categories/{ref}", lang=lang).json()

    def category_properties(self, ref: str, lang: str = "fr-FR") -> list[dict]:
        body = self._request("GET", f"/categories/{ref}/properties", lang=lang).json()
        return body.get("items", []) if isinstance(body, dict) else body


def _body(r: requests.Response):
    try:
        return r.json()
    except ValueError:
        return r.text
