"""TNG Stadtnetz: public availability form driven with Playwright.

The form is a Next.js / Headless-UI app. Inputs are comboboxes selected by `name`
(their ids are generated). The autocomplete list is virtualized; we scroll it to
find the desired street. Result pages are classified by `classify`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..fetch import Fetcher

BASE = "https://bestellung.tng.de/bestellstrecke/verfuegbarkeit?productId=PKGFSURF1000"
CITY = "Frankfurt am Main"
PAUSE_S = 5.0
_last_lookup = [0.0]


@dataclass(frozen=True)
class Result:
    status: str  # yes | no | unmatched | error
    detail: str | None
    raw: str | None


def pace() -> None:
    import time

    wait = _last_lookup[0] + PAUSE_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_lookup[0] = time.monotonic()


def fetch_result_html(
    street: str,
    house_number: str,
    plz: str,
) -> tuple[str, Optional[str]]:
    """Drive the form in a real browser and return (html, None) on success.

    Returns (None, detail) for an unmatched address or (None, error) for a failure.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "no-playwright"

    import shutil

    system_chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    try:
        with sync_playwright() as p:
            launch = {"headless": True}
            if system_chromium:
                launch["executable_path"] = system_chromium
            browser = p.chromium.launch(**launch)
            try:
                page = browser.new_page()
                page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_selector('input[name="zip-and-city"]', timeout=15000)

                # Cookiebot consent banner
                try:
                    page.click('#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll', timeout=5000)
                    page.wait_for_selector('#CybotCookiebotDialog', state='hidden', timeout=5000)
                except Exception:  # noqa: BLE001
                    pass

                # New customer
                page.locator('input[name="customer-type"][value="new"]').check()

                # PLZ + city
                inp = page.locator('input[name="zip-and-city"]')
                inp.click()
                inp.press_sequentially(plz, delay=80)
                page.wait_for_timeout(8000)
                try:
                    page.wait_for_selector('[role="option"]', timeout=15000)
                except Exception:  # noqa: BLE001 - no suggestion means outside TNG's footprint
                    return None, "no-plz-suggestion"
                opts = page.locator('[role="option"]')
                picked = False
                for i in range(opts.count()):
                    text = opts.nth(i).inner_text()
                    if plz in text:
                        opts.nth(i).click()
                        picked = True
                        break
                if not picked and opts.count():
                    opts.first.click()
                    picked = True
                if not picked:
                    return None, "no-plz-suggestion"

                # Street
                page.wait_for_selector('input[name="street"]:not([disabled])', timeout=10000)
                page.locator('input[name="street"]').press_sequentially(street, delay=50)
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_selector('[role="option"]', timeout=15000)
                except Exception:  # noqa: BLE001 - no street suggestion for this address
                    return None, "no-street-suggestion"

                picked = False
                lb = page.locator('[role="listbox"]')
                for _scroll in range(60):
                    opts = page.locator('[role="option"]')
                    for i in range(opts.count()):
                        text = opts.nth(i).inner_text().strip()
                        if street.casefold() in text.casefold():
                            opts.nth(i).click()
                            picked = True
                            break
                    if picked:
                        break
                    prev = lb.evaluate("el => el.scrollTop")
                    lb.evaluate("el => el.scrollTop += 200")
                    page.wait_for_timeout(250)
                    new = lb.evaluate("el => el.scrollTop")
                    if new == prev:
                        break
                if not picked:
                    return None, "no-street-suggestion"

                # House number
                page.wait_for_selector('input[name="housenumber"]:not([disabled])', timeout=10000)
                page.locator('input[name="housenumber"]').press_sequentially(house_number, delay=50)
                try:
                    page.wait_for_selector('[role="option"]', timeout=10000)
                    opts = page.locator('[role="option"]')
                    picked = False
                    for i in range(opts.count()):
                        text = opts.nth(i).inner_text()
                        if house_number in text:
                            opts.nth(i).click()
                            picked = True
                            break
                    if not picked and opts.count():
                        opts.first.click()
                        picked = True
                except Exception:  # noqa: BLE001
                    pass

                # Submit; TNG answers via a Next.js server action (POST).
                try:
                    with page.expect_response(lambda r: r.request.method == "POST", timeout=15000):
                        page.click('button[type="submit"]')
                except Exception:  # noqa: BLE001 - click anyway if no POST observed
                    page.click('button[type="submit"]')
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                try:
                    page.wait_for_url("**/wunschtarif", timeout=15000)
                except Exception:  # noqa: BLE001 - stayed on the form (no fiber)
                    pass
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(3000)
                html = page.content()
                return html, None
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        return None, f"playwright:{type(exc).__name__}:{str(exc)[:200]}"


def classify(html: str) -> tuple[str, str]:
    """Classify a captured TNG result page.

    Yes: the tariff page with "Tarif für Sie verfügbar" and occupancy selection.
    No:  landed on /wunschtarif but the tariff is not available, OR the form
         re-rendered after submit with no product offered.
    """
    # Next.js embeds error-message templates in every page's JS config; strip scripts.
    visible = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    visible = re.sub(r"<style.*?</style>", " ", visible, flags=re.S | re.I)
    low = re.sub(r"<[^>]+>", " ", visible).casefold()
    # Unmatched: address not found.
    if "die adresse ist uns leider nicht bekannt" in low or "adresse nicht eindeutig" in low:
        return "unmatched", "address-error"
    # Yes: tariff page with product offered.
    if "tarif für sie verfügbar" in low and "was trifft auf sie zu" in low:
        return "yes", "tariff-offered"
    # No: tariff not available (landed on /wunschtarif but no product).
    if "wunschtarif nicht verfügbar" in low or "tarif nicht verfügbar" in low:
        return "no", "tariff-unavailable"
    # No: form re-rendered after submit, no product offered.
    if "prüfen sie die verfügbarkeit an ihrer anschlussadresse" in low and "jetzt prüfen" in low:
        return "no", "no-tariff"
    # Anything else is an error, not a silent 'no'.
    return "error", "unclassified"


def stored_raw(html: str) -> str:
    """The stripped visible text (~3 KB) that measure() stores as raw."""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = title_m.group(1) if title_m else ""
    main_m = re.search(r"<main[^>]*>(.*?)</main>", html, re.S | re.I)
    body = main_m.group(1) if main_m else html
    visible = re.sub(r"<script.*?</script>", " ", body, flags=re.S | re.I)
    visible = re.sub(r"<style.*?</style>", " ", visible, flags=re.S | re.I)
    visible = re.sub(r"<[^>]+>", " ", visible)
    visible = re.sub(r"\s+", " ", visible).strip()
    return (title + " " + visible).strip()[:3000]


def measure(fetcher: Fetcher, street: str, house_number: str, plz: str | None) -> Result:
    if not plz:
        return Result("unmatched", "no-plz", None)
    pace()
    html, err = fetch_result_html(street, house_number, plz)
    if html is None:
        if err and err.startswith("no-"):
            return Result("unmatched", err, None)
        return Result("error", err or "no-page", None)
    status, detail = classify(html)
    return Result(status, detail, stored_raw(html))
