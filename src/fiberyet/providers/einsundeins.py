"""1&1: cookie-session address checker over plain HTTP.

Works over plain HTTP with a cookie session. Shows Telekom's, OXG's and Versatel's networks
without naming the owner. `gf-vvm` means fiber is orderable; `dsl-only-in-gf` means only
copper/DSL or fiber-DSL, not FTTH.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from ..fetch import Fetcher
from ..match import same_address

BASE = "https://www.1und1.de"
FORM_URL = BASE + "/glasfaser/"
CHECK_URL = BASE + "/frontend/vcheck/check-availability"
RESULT_URL = "https://dsl.1und1.de/flow2/f2-bb/dsl-verfuegbarkeit-ergebnis-1"
CITY = "Frankfurt am Main"


@dataclass(frozen=True)
class Result:
    status: str  # yes | no | unmatched | error
    detail: str | None
    raw: str | None


class BadRequest(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"http{status}")
        self.status = status
        self.body = body


def _check_response(resp: httpx.Response) -> dict:
    if resp.status_code != 200:
        raise BadRequest(resp.status_code, resp.text)
    return resp.json()


def measure(fetcher: Fetcher, street: str, house_number: str, plz: str | None) -> Result:
    if not plz:
        return Result("unmatched", "no-plz", None)

    # Ensure we have a session (cookies from the form page).
    if not fetcher.client.cookies:
        fetcher.get(FORM_URL)

    payload = {
        "zip": plz, "city": CITY, "street": street, "streetNumber": house_number,
        "action": "complete_check",
        "redirectUrl": "//dsl.1und1.de/flow2/f2-bb/dsl-verfuegbarkeit-ergebnis-1",
        "formUrl": "//www.1und1.de/glasfaser/",
        "realm": "FTTH_GF", "offerModel": "", "v-check-option": "option2",
    }
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json",
        "x-cdp-shop-scope": "dsl-nk",
        "Origin": BASE,
        "Referer": FORM_URL,
    }
    try:
        resp = fetcher.post(CHECK_URL, json=payload, headers=headers)
    except BadRequest as exc:
        if exc.status == 503 and not fetcher.client.cookies:
            raise  # second 503 without cookies: give up
        # first 503: drop cookies and retry once
        fetcher.client.cookies.clear()
        fetcher.get(FORM_URL)
        resp = fetcher.post(CHECK_URL, json=payload, headers=headers)

    data = _check_response(resp)
    if data.get("addressNotFound"):
        return Result("unmatched", "not-found", resp.text)
    if data.get("address", {}).get("partsOfTheBuilding") is not None:
        return Result("unmatched", "building-parts", resp.text)
    addr = data.get("address", {})
    if not same_address(street, house_number, addr.get("street", ""), addr.get("streetNumber", "")):
        return Result("unmatched", "mismatch", resp.text)

    # Fetch the result page and read the subheading class.
    page = fetcher.get(RESULT_URL)
    status, detail = classify(page.text)
    return Result(status, detail, page.text[:2000])


def classify(html: str) -> tuple[str, str]:
    """(status, detail) from the result page: the class of the first div inside the subheading."""
    m = re.search(r'<div class="[^"]*v-check__subheading[^"]*"[^>]*>\s*<div class="([\w-]+)"', html)
    if not m:
        return "error", "no-subheading"
    cls = m.group(1)
    if cls == "gf-vvm":
        return "yes", cls
    if cls == "dsl-only-in-gf":
        return "no", cls
    return "error", f"unknown:{cls}"
