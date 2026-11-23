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



def test_gf_only_is_yes():
    html = GALLERY % ("gf-only", "In 60528 Frankfurt am Main, Adolfstr. 23 C sind 1&1 Glasfaser-Tarife verfügbar.")
    assert einsundeins.classify(html) == ("yes", "gf-only")


def test_dsl_16_only_is_no():
    html = GALLERY % ("dsl-16-only-in-gf", "In 65929 Frankfurt am Main, Hochmuhl 5 ist ein 1&1 Glasfaser-Direktanschluss derzeit noch nicht verfügbar.")
    assert einsundeins.classify(html) == ("no", "dsl-16-only-in-gf")



def test_hotline_only_is_no():
    html = '<div class="section-headline__subline">In 60486 Frankfurt am Main, Voltastr. 79 a ist 1&1 DSL oder Glasfaser aktuell nur über unsere Bestell-Hotline verfügbar.</div>'
    assert einsundeins.classify(html) == ("no", "hotline-only")

def test_missing_plz_never_requests():
    f = Fetcher()
    try:
        r = einsundeins.measure(f, "Zeil", "106", None)
    finally:
        f.close()
    assert r == einsundeins.Result("unmatched", "no-plz", None)
