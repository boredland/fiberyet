"""TNG Stadtnetz: public availability form driven with Playwright.

The form is a Next.js / Headless-UI app. Inputs are comboboxes selected by `name`
(their ids are generated). The autocomplete list is `role="option"` inside a
`role="listbox"` that appears after typing. Result pages are classified by `classify`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..match import same_address
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
    debug_dir: Optional[Path] = None,
    debug_name: str = "tng",
) -> tuple[str, Optional[str]]:
    """Drive the form in a real browser and return (html, None) on success.

    Returns (None, detail) for an unmatched address or (None, error) for a failure.
    When ``debug_dir`` is given, intermediate pages are written there.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "no-playwright"

    import shutil

    def dump(page, label):
        if debug_dir is not None:
            try:
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / f"{debug_name}_{label}.html").write_text(page.content())
            except Exception:  # noqa: BLE001 - best effort
                pass

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
                dump(page, "0_loaded")
                # Cookiebot consent banner blocks focus/typing; accept it.
                try:
                    page.click('#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll', timeout=5000)
                    page.wait_for_selector('#CybotCookiebotDialog', state='hidden', timeout=5000)
                except Exception:  # noqa: BLE001 - no banner or already accepted
                    pass
                dump(page, "0b_after_consent")

                # New customer
                page.locator('input[name="customer-type"][value="new"]').check()

                # PLZ + city
                inp = page.locator('input[name="zip-and-city"]')
                inp.click()
                inp.press_sequentially(f"{plz} {CITY}", delay=80)
                page.wait_for_timeout(8000)
                dump(page, "1a_after_type")
                try:
                    page.keyboard.press("ArrowDown")
                    page.wait_for_timeout(2000)
                except Exception:  # noqa: BLE001
                    pass
                dump(page, "1b_after_nudge")
                page.wait_for_selector('[role="option"]', timeout=15000)
                opts = page.locator('[role="option"]')
                picked = False
                for i in range(opts.count()):
                    text = opts.nth(i).inner_text()
                    if plz in text and CITY.split()[0] in text:
                        opts.nth(i).click()
                        picked = True
                        break
                if not picked and opts.count():
                    opts.first.click()
                    picked = True
                if not picked:
                    dump(page, "1_plz_no_pick")
                    return None, "no-plz-suggestion"

                # Street
                page.wait_for_selector('input[name="street"]:not([disabled])', timeout=10000)
                page.locator('input[name="street"]').press_sequentially(street, delay=50)
                page.wait_for_selector('[role="option"]', timeout=10000)
                dump(page, "2_street_options")
                opts = page.locator('[role="option"]')
                picked = False
                for i in range(opts.count()):
                    text = opts.nth(i).inner_text()
                    s = text.split(",")[0].strip()
                    if same_address(street, house_number, s, house_number):
                        opts.nth(i).click()
                        picked = True
                        break
                if not picked:
                    # fall back to first option containing the street
                    for i in range(opts.count()):
                        if street.casefold() in opts.nth(i).inner_text().casefold():
                            opts.nth(i).click()
                            picked = True
                            break
                if not picked:
                    dump(page, "2_street_no_pick")
                    return None, "no-street-suggestion"

                # House number
                page.wait_for_selector('input[name="housenumber"]:not([disabled])', timeout=10000)
                page.locator('input[name="housenumber"]').press_sequentially(house_number, delay=50)
                try:
                    page.wait_for_selector('[role="option"]', timeout=10000)
                    dump(page, "3_number_options")
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
                    # some forms accept the typed number directly
                    pass

                dump(page, "4_before_submit")
                page.click('button[type="submit"]')
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:  # noqa: BLE001 - SPA may keep polling
                    pass
                dump(page, "5_result")
                html = page.content()
                return html, None
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - report and let the caller decide
        return None, f"playwright:{type(exc).__name__}:{str(exc)[:200]}"


def classify(html: str) -> tuple[str, str]:
    low = html.casefold()
    if "adresse nicht gefunden" in low or "adresse nicht eindeutig" in low or "keine adresse" in low:
        return "unmatched", "address-error"
    # Interest registration form (no fiber) vs. orderable product.
    if "interesse" in low and ("anmeld" in low or "registrier" in low or "vormerk" in low):
        return "no", "interest"
    if "bestellen" in low or "in den warenkorb" in low or "jetzt beauftragen" in low or ("verfügbar" in low and "glasfaser" in low):
        return "yes", "order"
    return "error", "unclassified"


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
    return Result(status, detail, html[:2000])
