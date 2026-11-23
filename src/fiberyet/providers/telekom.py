"""Telekom: the public coverage map's own address lookup (no cookies, no tokens).

`search/` resolves free text to an internal id and coordinates, `hpac/` lists
the products available there. A fiber product with displayStatus AVAILABLE is
what we count as "orderable FTTH".
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import httpx

from ..fetch import Blocked, Fetcher
from ..match import same_address

BASE = "https://t-map.telekom.de/tmap2/coverage_checker/"
CITY = "Frankfurt am Main"


@dataclass(frozen=True)
class Result:
    status: str  # yes | no | unmatched | error
    detail: str | None
    raw: str | None


class BadRequest(Exception):
    """The server refused this one query (4xx other than the block statuses). Not a block."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"http{status}")
        self.status = status
        self.body = body


def _json(resp: httpx.Response):
    """A 200 JSON answer is the contract. A 200 that is not JSON (a served or solved challenge) is a block;
    any other status here is a rejection of this single query, which says nothing about us being blocked."""
    if resp.status_code != 200:
        raise BadRequest(resp.status_code, resp.text)
    if not resp.headers.get("content-type", "").lower().startswith("application/json"):
        raise Blocked(0)
    try:
        return json.loads(resp.text)
    except ValueError:
        raise Blocked(0) from None


def to_web_mercator(lon: float, lat: float) -> tuple[float, float]:
    x = lon * 20037508.342789244 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) * 20037508.342789244 / math.pi
    return x, y


def pick_feature(search: dict, street: str, house_number: str) -> dict | None:
    for f in search.get("features", []):
        p = f.get("properties", {})
        if p.get("city") != CITY:
            continue
        if same_address(street, house_number, p.get("street", ""), p.get("housenumber", "")):
            return f
    return None


def classify(hpac: dict) -> tuple[str, str]:
    products = hpac.get("products", [])
    detail = ",".join(sorted(p.get("type", "") for p in products))
    fiber = any(p.get("type") == "AVAILABLE_FIBER" and p.get("displayStatus") == "AVAILABLE" for p in products)
    return ("yes" if fiber else "no"), detail


def measure(fetcher: Fetcher, street: str, house_number: str) -> Result:
    try:
        search_resp = fetcher.get(
            BASE + "search/", params={"type": "address", "q": f"{street} {house_number}, {CITY}"}
        )
        search = _json(search_resp)
        feature = pick_feature(search, street, house_number)
        if feature is None:
            return Result("unmatched", None, search_resp.text)
        lon, lat = feature["geometry"]["coordinates"][:2]
        x, y = to_web_mercator(lon, lat)
        hpac_resp = fetcher.get(BASE + "hpac/", params={"klsId": feature["properties"]["id"], "x": x, "y": y})
        status, detail = classify(_json(hpac_resp))
        return Result(status, detail, hpac_resp.text)
    except BadRequest as exc:
        return Result("error", f"http{exc.status}", exc.body[:2000])
