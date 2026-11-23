"""TNG Stadtnetz: public availability form driven with Playwright.

The form is a Next.js / Headless-UI app. Inputs are comboboxes selected by `name`
(their ids are generated). The autocomplete list is virtualized; we scroll it to
find the desired street. Result pages are classified by `classify`.
"""

from __future__ import annotations

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
                page.wait_for_selector('[role="option"]', timeout=15000)
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
                page.wait_for_selector('[role="option"]', timeout=15000)

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

                page.click('button[type="submit"]')
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:  # noqa: BLE001
                    pass
                print(f"TNG DEBUG url={page.url} title={page.title()}", flush=True)
                try:
                    print(f"TNG DEBUG body={page.locator('body').inner_text()[-800:]}", flush=True)
                except Exception:  # noqa: BLE001
                    pass
                html = page.content()
                return html, None
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        return None, f"playwright:{type(exc).__name__}:{str(exc)[:200]}"


def classify(html: str) -> tuple[str, str]:
    low = html.casefold()
    if "adresse nicht gefunden" in low or "adresse nicht eindeutig" in low or "keine adresse" in low:
        return "unmatched", "address-error"
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
