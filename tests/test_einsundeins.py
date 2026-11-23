"""1&1 result classification (plan step 6): the subheading div's class decides yes/no/error.

The exact page structures come from the plan's Findings — two addresses verified live on
2026-11-22 (Zeil 106 orderable, Alt Heddernheim 30 copper-only).
"""

from fiberyet.fetch import Fetcher
from fiberyet.providers import einsundeins

GALLERY = """
<div class="section-headline__headline v-check__heading">Ihr Check-Ergebnis:</div>
<div class="section-headline__subline v-check__subheading">
    <div class="%s">%s</div>
</div>
"""


def test_fiber_build_started_is_yes():
    html = GALLERY % ("gf-vvm", "In 60313 Frankfurt am Main, Zeil 106 hat der Glasfaserausbau bereits begonnen.")
    assert einsundeins.classify(html) == ("yes", "gf-vvm")


def test_copper_only_is_no():
    html = GALLERY % ("dsl-only-in-gf", "In 60439 Frankfurt am Main, Alt Heddernheim 30 ist ein Glasfaser-Direktanschluss noch nicht verfügbar.")
    assert einsundeins.classify(html) == ("no", "dsl-only-in-gf")


def test_unknown_class_is_an_error_not_a_guess():
    html = GALLERY % ("unverified", "some wording")
    assert einsundeins.classify(html) == ("error", "unknown:unverified")


def test_missing_subheading_is_an_error():
    assert einsundeins.classify("<html><body>nothing here</body></html>") == ("error", "no-subheading")


def test_missing_plz_never_requests():
    f = Fetcher()
    try:
        r = einsundeins.measure(f, "Zeil", "106", None)
    finally:
        f.close()
    assert r == einsundeins.Result("unmatched", "no-plz", None)
