"""TNG Stadtnetz: public availability form driven with Playwright.

Only covers TNG's eight districts. The result pages were not observed, so classification
follows the plan's rule: `yes` when the product is offered for order (occupancy selection
follows), `no` when the interest-registration form appears, `unmatched` on address errors.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..match import same_address
from ..fetch import Fetcher

BASE = "https://bestellung.tng.de/bestellstrecke/verfuegbarkeit?productId=PKGFSURF1000"
CITY = "Frankfurt am Main"


@dataclass(frozen=True)
class Result:
    status: str  # yes | no | unmatched | error
    detail: str | None
    raw: str | None


def measure(fetcher: Fetcher, street: str, house_number: str, plz: str | None) -> Result:
    if not plz:
        return Result("unmatched", "no-plz", None)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return Result("error", "no-playwright", None)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(BASE, wait_until="networkidle", timeout=30000)

            # Select "new customer"
            page.locator('input[name="customer-type"][value="new"]').check()

            # Fill the address
            page.fill("#zip-and-city", f"{plz} {CITY}")
            page.fill("#street", street)
            page.fill("#housenumber", house_number)

            # Pick the autocomplete suggestion that matches
            page.wait_for_selector(".autocomplete-suggestions", timeout=5000)
            suggestions = page.locator(".autocomplete-suggestion")
            picked = False
            for i in range(suggestions.count()):
                text = suggestions.nth(i).inner_text()
                # Compare just the street name part (before the comma)
                s = text.split(",")[0].strip()
                if same_address(street, house_number, s, house_number):
                    suggestions.nth(i).click()
                    picked = True
                    break
            if not picked:
                browser.close()
                return Result("unmatched", "no-suggestion", None)

            # Submit
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle", timeout=15000)

            html = page.content()
            browser.close()
    except Exception as exc:
        return Result("error", f"playwright:{type(exc).__name__}", None)

    # Classify the result page.
    # "yes" = product offered for order (occupancy selection follows)
    # "no" = interest registration form
    # "unmatched" = address not found / not unique
    if "adresse nicht gefunden" in html.lower() or "adresse nicht eindeutig" in html.lower():
        return Result("unmatched", "address-error", html[:2000])
    if "interesse" in html.lower() and "anmelden" in html.lower():
        return Result("no", "interest", html[:2000])
    if "bestellen" in html.lower() or "auswahl" in html.lower() or "tarif" in html.lower():
        return Result("yes", "order", html[:2000])
    return Result("error", "unclassified", html[:2000])
