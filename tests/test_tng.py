"""TNG result classification (plan step 7).

The two fixtures are real result pages captured from the live form on 2026-11-23:
- tng_a.html: Alt-Heddernheim 30 → tariff page ("Tarif für Sie verfügbar", yes)
- tng_b.html: Römerberg 23 → availability form re-rendered (no product offered, no)
"""

from pathlib import Path

from fiberyet.providers import tng

FIXTURES = Path(__file__).parent / "fixtures"


def test_tariff_page_is_yes():
    html = (FIXTURES / "tng_a.html").read_text()
    assert tng.classify(html) == ("yes", "tariff-offered")


def test_form_rerender_is_no():
    html = (FIXTURES / "tng_b.html").read_text()
    assert tng.classify(html) == ("no", "no-tariff")


def test_address_error_is_unmatched():
    html = "<html>Die Adresse ist uns leider nicht bekannt.</html>"
    assert tng.classify(html) == ("unmatched", "address-error")


def test_unknown_page_is_error():
    assert tng.classify("<html><body>unexpected</body></html>") == ("error", "unclassified")
