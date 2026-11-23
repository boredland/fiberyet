"""TNG Stadtnetz: public availability form driven with Playwright.

Only covers TNG's eight districts. The result pages were not observed live yet; `classify`
follows the plan's rule — `yes` when the product is offered for order (occupancy selection
follows), `no` when the interest-registration form appears, `unmatched` on address errors —
and is checked against captured fixtures in tests/fixtures/tng_*.html.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass

from ..fetch import Fetcher
from ..match import same_address

BASE = "https://bestellung.tng.de/bestellstrecke/verfuegbarkeit?productId=PKGFSURF1000"
CITY = "Frankfurt am Main"
# Playwright loads bypass the Fetcher's per-host limiter, so pace lookups here (plan: 5 s apart).
PAUSE_S = 5.0
_last_lookup = [0.0]


@dataclass(frozen=True)
class Result:
    status: str  # yes | no | unmatched | error
    detail: str | None
    raw: str | None


def pace() -> None:
    wait = _last_lookup[0] + PAUSE_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_lookup[0] = time.monotonic()


def fetch_result_html(street: str, house_number: str, plz: str) -> tuple[str | None, str | None]:
    """Drive the availability form once. Returns (html, error_detail); html is None on error.

    Unmatched conditions that never reach a result page (no suggestion) raise ValueError with
    the plan's `unmatched` detail, because they are answers, not failures.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "no-playwright"

    # The bundled Chromium needs system libraries; the distro's chromium package has its own.
    system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")

    with sync_playwright() as p:
        launch = {"headless": True}
        if system_chromium:
            launch["executable_path"] = system_chromium
        browser = p.chromium.launch(**launch)
        try:
            page = browser.new_page()
            page.goto(BASE, wait_until="networkidle", timeout=30000)

            page.locator('input[name="customer-type"][value="new"]').check()
            page.fill("#zip-and-city", f"{plz} {CITY}")
            page.fill("#street", street)
            page.fill("#housenumber", house_number)

            page.wait_for_selector(".autocomplete-suggestions", timeout=5000)
            suggestions = page.locator(".autocomplete-suggestion")
            picked = False
            for i in range(suggestions.count()):
                s = suggestions.nth(i).inner_text().split(",")[0].strip()
                if same_address(street, house_number, s, house_number):
                    suggestions.nth(i).click()
                    picked = True
                    break
            if not picked:
                raise ValueError("no-suggestion")

            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle", timeout=15000)
            return page.content(), None
        finally:
            browser.close()


def classify(html: str) -> tuple[str, str]:
    """(status, detail) from a captured result page."""
    low = html.lower()
    if "adresse nicht gefunden" in low or "adresse nicht eindeutig" in low:
        return "unmatched", "address-error"
    if "interesse" in low and "anmelden" in low:
        return "no", "interest"
    if "bestellen" in low or "auswahl" in low or "tarif" in low:
        return "yes", "order"
    return "error", "unclassified"


def measure(fetcher: Fetcher, street: str, house_number: str, plz: str | None) -> Result:
    if not plz:
        return Result("unmatched", "no-plz", None)
    pace()
    try:
        html, err = fetch_result_html(street, house_number, plz)
    except ValueError as exc:
        return Result("unmatched", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - any browser failure is a recorded error, not a crash
        return Result("error", f"playwright:{type(exc).__name__}", None)
    if html is None:
        return Result("error", err or "no-page", None)
    status, detail = classify(html)
    return Result(status, detail, html[:2000])
