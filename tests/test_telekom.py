
import httpx
import pytest

from fiberyet.fetch import Blocked, Fetcher
from fiberyet.providers import telekom

SEARCH = {
    "features": [
        {
            "geometry": {"coordinates": [8.67321, 50.11955], "type": "Point"},
            "type": "Feature",
            "properties": {
                "id": "3689399",
                "city": "Frankfurt am Main",
                "street": "Gärtnerweg",
                "state": "Hessen",
                "housenumber": "48",
                "postcode": "60322",
            },
        },
        {
            "geometry": {"coordinates": [8.67321, 50.11955], "type": "Point"},
            "type": "Feature",
            "properties": {
                "id": "3689400",
                "city": "Frankfurt am Main",
                "street": "Gärtnerweg",
                "state": "Hessen",
                "housenumber": "48A",
                "postcode": "60322",
            },
        },
    ],
    "type": "FeatureCollection",
}
FIBER = {
    "products": [
        {"displayStatus": "AVAILABLE", "type": "AVAILABLE_FIBER", "name": "Glasfaser 1.000"},
    ]
}
COPPER = {
    "products": [
        {"displayStatus": "AVAILABLE", "type": "AVAILABLE_COPPER", "name": "MagentaZuhause XL"},
        {"displayStatus": "INTEREST", "type": "INTEREST", "name": "Highspeed"},
    ]
}


def fetcher_for(handler) -> Fetcher:
    return Fetcher(pause_s=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def json_response(obj, status=200):
    return httpx.Response(status, json=obj)


def test_pick_feature_by_number_and_suffix():
    assert telekom.pick_feature(SEARCH, "Gärtnerweg", "48")["properties"]["id"] == "3689399"
    assert telekom.pick_feature(SEARCH, "Gärtnerweg", "48a")["properties"]["id"] == "3689400"
    assert telekom.pick_feature(SEARCH, "Gärtnerweg", "47") is None


def test_classify():
    assert telekom.classify(FIBER) == ("yes", "AVAILABLE_FIBER")
    assert telekom.classify(COPPER) == ("no", "AVAILABLE_COPPER,INTEREST")


def test_planned_fiber_is_not_orderable():
    planned = {"products": [{"displayStatus": "PLANNED", "type": "PLANNED_FIBER_AREA"}]}
    assert telekom.classify(planned)[0] == "no"


def test_measure_two_requests_yes():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.path)
        return json_response(SEARCH if request.url.path.endswith("search/") else FIBER)

    result = telekom.measure(fetcher_for(handler), "Gärtnerweg", "48")
    assert (result.status, result.detail) == ("yes", "AVAILABLE_FIBER")
    assert len(seen) == 2


def test_measure_unmatched_stops_after_search():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        return json_response(SEARCH)

    result = telekom.measure(fetcher_for(handler), "Gärtnerweg", "47")
    assert result.status == "unmatched" and len(calls) == 1


def test_html_answer_with_200_is_a_block():
    def handler(request: httpx.Request):
        return httpx.Response(200, text="<html>Just a moment...</html>", headers={"content-type": "text/html"})

    with pytest.raises(Blocked) as exc:
        telekom.measure(fetcher_for(handler), "Gärtnerweg", "48")
    assert exc.value.status == 0


def test_json_content_type_with_broken_body_is_a_block():
    def handler(request: httpx.Request):
        return httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})

    with pytest.raises(Blocked):
        telekom.measure(fetcher_for(handler), "Gärtnerweg", "48")


@pytest.mark.parametrize("status", [403, 429])
def test_block_statuses_stop_without_retry(status):
    calls = []

    def handler(request: httpx.Request):
        calls.append(1)
        return httpx.Response(status)

    with pytest.raises(Blocked) as exc:
        telekom.measure(fetcher_for(handler), "Gärtnerweg", "48")
    assert exc.value.status == status and len(calls) == 1


def test_5xx_is_retried_three_times_then_unavailable():
    from fiberyet.fetch import Unavailable

    sleeps = []
    calls = []

    def handler(request: httpx.Request):
        calls.append(1)
        return httpx.Response(503)

    f = Fetcher(pause_s=0, transport=httpx.MockTransport(handler), sleep=sleeps.append)
    with pytest.raises(Unavailable):
        telekom.measure(f, "Gärtnerweg", "48")
    assert len(calls) == 4 and sleeps == [30, 60, 120]


def test_error_text_carries_no_url_or_address():
    def handler(request: httpx.Request):
        raise httpx.ConnectError("boom", request=request)

    from fiberyet.fetch import Unavailable

    f = Fetcher(pause_s=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(Unavailable) as exc:
        telekom.measure(f, "Gärtnerweg", "48")
    text = str(exc.value)
    assert "://" not in text and "Gärtnerweg" not in text


def test_limiter_enforces_pause_between_requests():
    from fiberyet.fetch import Limiter

    now = [100.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    lim = Limiter(3.0, clock=lambda: now[0], sleep=sleep)
    lim.wait()
    now[0] += 1.0
    lim.wait()
    assert slept == [2.0]


def test_web_mercator_matches_pyproj():
    from pyproj import Transformer

    ref = Transformer.from_crs(4326, 3857, always_xy=True)
    for lon, lat in [(8.64882, 50.15816), (8.55236, 50.10114), (8.682, 50.111)]:
        x, y = telekom.to_web_mercator(lon, lat)
        rx, ry = ref.transform(lon, lat)
        assert abs(x - rx) < 0.01 and abs(y - ry) < 0.01
