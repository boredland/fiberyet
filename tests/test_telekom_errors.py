import httpx
import pytest

from fiberyet.fetch import Blocked, Fetcher
from fiberyet.providers import telekom

SEARCH_OK = {
    "features": [
        {
            "geometry": {"coordinates": [8.67321, 50.11955], "type": "Point"},
            "properties": {"id": "1", "city": "Frankfurt am Main", "street": "Gärtnerweg", "housenumber": "48"},
        }
    ]
}


def fetcher_for(handler) -> Fetcher:
    return Fetcher(pause_s=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_400_problem_json_is_an_error_row_not_a_block():
    def handler(request: httpx.Request):
        return httpx.Response(400, json={"title": "bad request"}, headers={"content-type": "application/problem+json"})

    r = telekom.measure(fetcher_for(handler), "Gärtnerweg", "48 ¾")
    assert r.status == "error" and r.detail == "http400" and "bad request" in r.raw


def test_404_on_second_call_is_an_error_row():
    def handler(request: httpx.Request):
        if request.url.path.endswith("search/"):
            return httpx.Response(200, json=SEARCH_OK)
        return httpx.Response(404, text="nope")

    r = telekom.measure(fetcher_for(handler), "Gärtnerweg", "48")
    assert r.status == "error" and r.detail == "http404"


def test_401_is_a_block():
    with pytest.raises(Blocked) as exc:
        telekom.measure(fetcher_for(lambda request: httpx.Response(401)), "Gärtnerweg", "48")
    assert exc.value.status == 401


def test_200_html_is_still_a_block():
    def handler(request: httpx.Request):
        return httpx.Response(200, text="<html>challenge</html>", headers={"content-type": "text/html"})

    with pytest.raises(Blocked) as exc:
        telekom.measure(fetcher_for(handler), "Gärtnerweg", "48")
    assert exc.value.status == 0
