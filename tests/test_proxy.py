import httpx
import pytest

from fiberyet.fetch import (
    Blocked,
    Fetcher,
    PlainProxyTransport,
    ProxyConfigError,
    proxy_transport_from_env,
)
from fiberyet.providers import telekom


def test_proxy_rewrites_to_plain_tier_only():
    seen = []

    def inner(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"features": []})

    transport = PlainProxyTransport("https://proxy.example/", "secret", httpx.MockTransport(inner))
    f = Fetcher(pause_s=0, transport=transport, sleep=lambda s: None)
    f.get("https://t-map.telekom.de/tmap2/coverage_checker/search/", params={"type": "address", "q": "x"})
    req = seen[0]
    assert req.url.host == "proxy.example" and req.method == "GET"
    params = dict(req.url.params)
    assert set(params) == {"url", "auto"} and params["auto"] == "0"
    assert params["url"].startswith("https://t-map.telekom.de/")
    assert req.headers["authorization"] == "Bearer secret"
    assert req.headers["user-agent"] == "ftth-availability-research/0.1"
    for forbidden in ("solve", "render", "wait", "session"):
        assert forbidden not in params


def test_proxy_401_is_a_block():
    transport = PlainProxyTransport("https://proxy.example/", "bad", httpx.MockTransport(lambda r: httpx.Response(401)))
    f = Fetcher(pause_s=0, transport=transport, sleep=lambda s: None)
    with pytest.raises(Blocked) as exc:
        f.get("https://t-map.telekom.de/x")
    assert exc.value.status == 401


def test_solved_challenge_html_through_proxy_trips_the_telekom_stop_rule():
    def inner(request: httpx.Request):
        return httpx.Response(200, text="<html>solved page</html>", headers={"content-type": "text/html; charset=utf-8"})

    transport = PlainProxyTransport("https://proxy.example/", "s", httpx.MockTransport(inner))
    f = Fetcher(pause_s=0, transport=transport, sleep=lambda s: None)
    with pytest.raises(Blocked) as exc:
        telekom.measure(f, "Römerberg", "23")
    assert exc.value.status == 0


def test_env_gate(monkeypatch):
    for k in ("FIBERYET_PROXY", "FETCH_PROXY_URL", "FETCH_PROXY_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert proxy_transport_from_env("telekom") is None

    monkeypatch.setenv("FIBERYET_PROXY", "plain")
    with pytest.raises(ProxyConfigError):
        proxy_transport_from_env("telekom")  # no URL or token
    monkeypatch.setenv("FETCH_PROXY_URL", "https://proxy.example/")
    monkeypatch.setenv("FETCH_PROXY_TOKEN", "t")
    assert isinstance(proxy_transport_from_env("telekom"), PlainProxyTransport)
    for other in ("einsundeins", "tng"):
        with pytest.raises(ProxyConfigError):
            proxy_transport_from_env(other)


def test_other_proxy_modes_are_ignored(monkeypatch):
    monkeypatch.setenv("FIBERYET_PROXY", "auto")
    assert proxy_transport_from_env("telekom") is None
