"""Polite HTTP for provider crawls: one request at a time, fixed pause, hard stop on blocks.

Nothing here prints a URL, query string, body or address: Actions logs of a
public repository are public.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import httpx

USER_AGENT = "ftth-availability-research/0.1"
BLOCK_STATUSES = (401, 403, 429)
DEFAULT_PAUSE_S = 3.0
RETRY_WAITS_S = (30, 60, 120)


class Blocked(Exception):
    """The provider refused us (401/403/429, challenge, or a non-JSON answer where JSON is the contract)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"blocked status={status}")
        self.status = status


class Unavailable(Exception):
    """Persistent 5xx or timeouts after the fixed retries; the run stops, nothing is recorded as a block."""


class ProxyConfigError(Exception):
    pass


def pause_seconds(state_pause: float | None = None) -> float:
    if state_pause is not None:
        return state_pause
    env = os.environ.get("CRAWL_PAUSE_S")
    return float(env) if env else DEFAULT_PAUSE_S


class PlainProxyTransport(httpx.BaseTransport):
    """Send every request through the fetch-proxy's REST `?url=` endpoint, plain tier only.

    `auto=0` is the only extra parameter, ever: it pins the plain fetch even when
    the instance enables auto-escalation. Never auto=1, solve, render, wait or session.
    """

    def __init__(self, base_url: str, token: str, inner: httpx.BaseTransport | None = None) -> None:
        self._base = httpx.URL(base_url)
        self._token = token
        self._inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        proxied_url = self._base.copy_merge_params({"url": str(request.url), "auto": "0"})
        headers = httpx.Headers({"User-Agent": USER_AGENT, "Authorization": f"Bearer {self._token}"})
        if accept := request.headers.get("accept"):
            headers["Accept"] = accept
        return self._inner.handle_request(httpx.Request(request.method, proxied_url, headers=headers))

    def close(self) -> None:
        self._inner.close()


def proxy_transport_from_env(provider: str, inner: httpx.BaseTransport | None = None) -> httpx.BaseTransport | None:
    """None unless FIBERYET_PROXY == 'plain'. Telekom only: other providers need cookies or a browser."""
    if os.environ.get("FIBERYET_PROXY") != "plain":
        return None
    if provider != "telekom":
        raise ProxyConfigError("proxy is Telekom-only (cookie passthrough unverified; TNG needs a browser)")
    url, token = os.environ.get("FETCH_PROXY_URL"), os.environ.get("FETCH_PROXY_TOKEN")
    if not url or not token:
        raise ProxyConfigError("FIBERYET_PROXY=plain needs FETCH_PROXY_URL and FETCH_PROXY_TOKEN")
    return PlainProxyTransport(url, token, inner)


class Limiter:
    """At least `pause_s` between the starts of consecutive requests to one host."""

    def __init__(
        self,
        pause_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.pause_s = pause_s
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.pause_s - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


class Fetcher:
    """One httpx client plus limiter. `get`/`post` raise Blocked or Unavailable, never anything with a URL in it."""

    def __init__(
        self,
        pause_s: float | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
            transport=transport,
            follow_redirects=True,
        )
        self._sleep = sleep
        self.limiter = Limiter(pause_seconds(pause_s), clock=clock, sleep=sleep)

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(len(RETRY_WAITS_S) + 1):
            self.limiter.wait()
            try:
                resp = self.client.request(method, url, **kwargs)
            except httpx.TransportError:
                resp = None
            if resp is not None:
                if resp.status_code in BLOCK_STATUSES:
                    raise Blocked(resp.status_code)
                if resp.status_code < 500:
                    return resp
            if attempt == len(RETRY_WAITS_S):
                raise Unavailable("persistent 5xx or timeout")
            self._sleep(RETRY_WAITS_S[attempt])
        raise Unavailable("unreachable")  # pragma: no cover

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)
