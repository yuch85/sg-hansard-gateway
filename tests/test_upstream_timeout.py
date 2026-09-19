"""F-5 (wave 6) rectify: per-attempt upstream timeout cap.

A stalled SPRS (the ~35s host-side stall observed live) must degrade to a
bounded 5xx-with-nav, not a hang. The 3x retry + backoff policy stays; each
ATTEMPT is capped at ``settings.upstream_attempt_timeout_s`` (15s). Offline:
respx simulates the stall with a sleep longer than the cap, and the test
asserts the response returns in bounded time with the 502 envelope.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import hansard_gateway.auth as auth_mod
from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import Settings
from hansard_gateway.main import create_app

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"

E2E_REPORT_ID = "037_20041019_S0004_T0023"

#: The stall the upstream exhibited live (~35s host-side) — the respx
#: side_effect sleeps this long; the per-attempt cap must cut it short.
_STALL_S = 2.0

#: Upper bound on the whole response (3 capped attempts + 2x 0.5s backoff
#: = ~3.1s theoretical; 10s leaves headroom for CI without being loose).
_BOUND_S = 10.0


@pytest.fixture()
def client(token_store) -> TestClient:
    """The full app wired to the fixture token store."""
    auth_mod._store = token_store
    return TestClient(create_app())


async def _stall(_request: httpx.Request) -> httpx.Response:
    """Simulate a stalled upstream: sleep past the per-attempt cap."""
    await asyncio.sleep(_STALL_S)
    return httpx.Response(200, json={})


def test_stalled_upstream_bounded_502(client: TestClient) -> None:
    """A stalled upstream returns in bounded time with the 502 envelope —
    not a hang. The cap (15s) makes each attempt fail fast; the retry
    budget (3) is exhausted and the route degrades to 502-with-nav."""
    cap = Settings().upstream_attempt_timeout_s
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").mock(side_effect=_stall)
        start = time.monotonic()
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=json")
        elapsed = time.monotonic() - start
    assert resp.status_code == 502
    assert resp.headers["content-type"].startswith("application/json")
    err = json.loads(resp.text)["error"]
    assert err["code"] == "upstream_unavailable"
    assert err["retryable"] is True
    # Bounded: far under the pre-fix 20s read timeout x 3 attempts (60s+).
    assert elapsed < _BOUND_S, f"stalled upstream took {elapsed:.1f}s (unbounded?)"
    # And the cap itself is documented + sane (10-15s per attempt).
    assert 10.0 <= cap <= 15.0


def test_stalled_upstream_html_502_with_nav(client: TestClient) -> None:
    """The html 502 carries the nav bar (fast 5xx-with-nav, not a hang)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").mock(side_effect=_stall)
        start = time.monotonic()
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
        elapsed = time.monotonic() - start
    assert resp.status_code == 502
    assert resp.headers["content-type"].startswith("text/html")
    # The valid-token error page carries working token-bearing nav links.
    assert f"/a/{TEST_TOKEN}/" in resp.text
    assert elapsed < _BOUND_S


def test_per_attempt_cap_wired() -> None:
    """The F-5 contract: each upstream attempt is capped at
    ``upstream_attempt_timeout_s`` (NOT the legacy 20s read timeout).

    respx's side_effect bypasses httpx timeouts (it is a transport mock), so
    the stall tests above bound time via the sleep. This test proves the cap
    is actually WIRED onto the per-call timeout by spying the timeout object
    the client passes to ``post`` on a real attempt."""
    import dataclasses

    import hansard_gateway.config as config_mod
    import hansard_gateway.sprs.client as cmod

    cfg = dataclasses.replace(Settings(), upstream_attempt_timeout_s=0.3)
    saved = config_mod.settings
    config_mod.settings = cfg
    cmod._settings = cfg
    captured: list[object] = []
    client = create_app(app_settings=cfg)

    # Spy the timeout passed to the underlying httpx post on ONE attempt.
    real_post_once = cmod.SprsClient._post_once

    async def spy_post_once(self, *, url, body):
        real_post = self._client.post

        async def cap_post(*a: object, **kw: object):
            captured.append(kw.get("timeout"))
            return await real_post(*a, **kw)

        self._client.post = cap_post  # type: ignore[method-assign]
        try:
            return await real_post_once(self, url=url, body=body)
        finally:
            self._client.post = real_post

    cmod.SprsClient._post_once = spy_post_once

    async def run() -> None:
        async with respx.mock(
            base_url=UPSTREAM_BASE, assert_all_called=False
        ) as mock:
            mock.post("/getHansardTopic").mock(
                side_effect=lambda req: _stall(req))
            tc = TestClient(client)
            tc.get(
                f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=json"
            )

    try:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(run())
    finally:
        cmod.SprsClient._post_once = real_post_once
        config_mod.settings = saved

    assert captured, "no attempt captured — the spy never fired"
    for t in captured:
        assert t is not None and getattr(t, "read", None) == 0.3, (
            f"per-attempt cap not applied (got read={getattr(t, 'read', '?')})"
        )


def _connect_timeout(_request: httpx.Request) -> None:
    """Simulate a connect stall: raise httpx.ConnectTimeout (a
    ``httpx.TransportError`` that is NOT a ``httpx.TimeoutException``).

    F-5b: this is the exact failure class the §8.5 live smoke hit — a
    connect/read stall that, pre-fix, escaped the retry loop (which only
    retried HTTP-status failures) and hung with ZERO retries."""
    raise httpx.ConnectTimeout("connect timed out")


def test_connect_stall_burns_retry_budget_502(client: TestClient) -> None:
    """A connect stall (httpx.ConnectTimeout) is RETRIED with backoff until
    the 3x budget is exhausted, then degrades to a bounded 502 envelope —
    it must NOT escape the retry loop and hang.

    F-5b: the live §8.5 smoke hit exactly this — the search hop returned
    'gateway timeout' while concurrent host probes of the same URL returned
    200 in 70-150ms. Root cause: _post_once wrapped the ConnectTimeout in
    UpstreamError, but the _post retry loop only retried HTTP-status
    failures, so the exception escaped and the client saw one long stall.
    """
    attempts = Settings().retry_attempts
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        route = mock.post("/getHansardTopic").mock(
            side_effect=_connect_timeout)
        start = time.monotonic()
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=json")
        elapsed = time.monotonic() - start
    # The route degrades to the bounded 502 envelope (not a hang, not a 500).
    assert resp.status_code == 502
    err = json.loads(resp.text)["error"]
    assert err["code"] == "upstream_unavailable"
    assert err["retryable"] is True
    # F-5b proof: the stall was RETRIED the full budget (attempts >= 3),
    # not escaped after one. Pre-fix this was exactly 1 call.
    assert route.call_count == attempts, (
        f"connect stall burned {route.call_count} of {attempts} retries "
        "(it must exhaust the budget, not escape the loop)"
    )
    # Bounded: 3 near-instant attempts + 2x 0.5s backoff ≈ 1s; 10s headroom.
    assert elapsed < _BOUND_S
