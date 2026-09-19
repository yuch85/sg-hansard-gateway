"""Auth tests: 404-not-401, no enumeration, no token upstream, masked repr.

The app routes under `/a/{token}/...` so FastAPI passes the path param `token`
by name into the `require_capability_token` dependency (RESEARCH Finding 7).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import respx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient

import hansard_gateway.auth as auth_mod
from hansard_gateway.auth import (
    TEST_TOKEN,
    TEST_TOKEN_LABEL,
    AuthContext,
    TokenRejected,
    require_capability_token,
)
from hansard_gateway.config import Settings
from hansard_gateway.rate_limit import RateGate
from hansard_gateway.sprs.client import SprsClient

NOT_FOUND_HTML = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
    "<title>Not Found</title></head><body><h1>Not Found</h1>"
    "<p>The requested resource was not found.</p></body></html>"
)


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(TokenRejected)
    async def _reject(_: Request, __: TokenRejected) -> HTMLResponse:
        return HTMLResponse(NOT_FOUND_HTML, status_code=404)

    @app.get(
        "/a/{token}/report/{report_id}",
        dependencies=[Depends(require_capability_token)],
    )
    async def _report(token: str, report_id: str):
        store = auth_mod._store
        assert store is not None
        ctx = await require_capability_token(token=token, store=store)
        gate = RateGate(settings=Settings())
        client = SprsClient(settings=Settings(), gate=gate, token_label=ctx.token_label)
        try:
            topic = await client.fetch_topic(report_id=report_id)
        finally:
            await client.aclose()
        return JSONResponse({"label": ctx.token_label, "id": topic.get("reportId")})

    return app


@pytest.fixture()
def client(token_store) -> TestClient:
    auth_mod._store = token_store
    return TestClient(_build_app())


def test_valid_token(client: TestClient) -> None:
    """A valid enabled token yields an AuthContext with the correct label."""
    with respx.mock(base_url="https://sprs.parl.gov.sg", assert_all_called=False) as mock:
        mock.post("/search/getHansardTopic").respond(
            json={"resultHTML": {"reportId": "bill-742#"}}
        )
        resp = client.get(f"/a/{TEST_TOKEN}/report/bill-742")
        assert resp.status_code == 200
        assert resp.json()["label"] == TEST_TOKEN_LABEL


def test_invalid_token_404_no_upstream(client: TestClient) -> None:
    """An invalid token → 404 with zero upstream calls."""
    with respx.mock(base_url="https://sprs.parl.gov.sg", assert_all_called=False) as mock:
        route = mock.post("/search/getHansardTopic").respond(json={})
        resp = client.get("/a/hg_notatoken0123456789abcdef/report/bill-742")
        assert resp.status_code == 404
        assert route.call_count == 0


def test_revoked_token_404_identical(client: TestClient) -> None:
    """A revoked (disabled) token → byte-identical 404 to an invalid token."""
    with respx.mock(base_url="https://sprs.parl.gov.sg", assert_all_called=False) as mock:
        mock.post("/search/getHansardTopic").respond(json={})
        revoked = client.get("/a/hg_revokedtoken0123456789abcdef/report/bill-742")
        invalid = client.get("/a/hg_notatoken0123456789abcdef/report/bill-742")
        assert revoked.status_code == 404
        assert invalid.status_code == 404
        assert revoked.content == invalid.content


def test_malformed_token_short_circuits(token_store) -> None:
    """A malformed (wrong-shape) token raises TokenRejected before hashing."""

    async def _run():
        await require_capability_token(token="not-a-capability-token", store=token_store)

    with pytest.raises(TokenRejected):
        import asyncio

        asyncio.new_event_loop().run_until_complete(_run())


def test_no_token_upstream(client: TestClient) -> None:
    """The recorded upstream request contains no `hg_` token in headers/body."""
    with respx.mock(base_url="https://sprs.parl.gov.sg", assert_all_called=False) as mock:
        route = mock.post("/search/getHansardTopic").respond(
            json={"resultHTML": {"reportId": "bill-742#"}}
        )
        resp = client.get(f"/a/{TEST_TOKEN}/report/bill-742")
        assert resp.status_code == 200
        assert route.called
        request = route.calls.last.request
        headers = {k.lower(): v for k, v in request.headers.items()}
        body = request.content.decode("utf-8")
        assert "hg_" not in body
        for value in headers.values():
            assert "hg_" not in value
        assert headers["user-agent"].startswith("Mozilla/5.0")
        assert headers["referer"] == "https://sprs.parl.gov.sg/search/"


def test_no_token_upstream_search(client: TestClient) -> None:
    """Plan 04: the /search path forwards no `hg_` token to any upstream.

    Builds the FULL app (the module's minimal app has no /search route) and
    covers both the SPRS searchResult POST and the Pair backend POST — every
    recorded request (headers + body) must be free of the capability token
    (addendum §19, release-blocking).
    """
    import json as _json
    from pathlib import Path

    from hansard_gateway.main import create_app

    full_client = TestClient(create_app())
    fixture = _json.loads(
        (Path(__file__).parent / "fixtures" / "searchresult_20041019_p1.json").read_text()
    )
    with respx.mock(base_url="https://sprs.parl.gov.sg/search", assert_all_called=False) as mock:
        sprs_route = mock.post("/searchResult").respond(json=fixture)
        with respx.mock(base_url="https://search.pair.gov.sg", assert_all_called=False,
                        assert_all_mocked=False) as pmock:
            pair_route = pmock.post("/api/v1/search").respond(json={}, status_code=201)
            resp = full_client.get(f"/a/{TEST_TOKEN}/search?q=Pension%20Fund")
    assert resp.status_code == 200
    assert sprs_route.called
    for route in (sprs_route, pair_route):
        for call in route.calls:
            request = call.request
            headers = {k.lower(): v for k, v in request.headers.items()}
            body = request.content.decode("utf-8")
            assert TEST_TOKEN not in body
            assert "hg_" not in body
            for value in headers.values():
                assert TEST_TOKEN not in value
                assert "hg_" not in value


def test_authcontext_repr_masks_token() -> None:
    """AuthContext.__repr__ never leaks the plaintext token."""
    ctx = AuthContext(token_label=TEST_TOKEN_LABEL, token=TEST_TOKEN)
    assert TEST_TOKEN not in repr(ctx)
    assert TEST_TOKEN not in str(ctx)
    assert "****" in repr(ctx)
    assert TEST_TOKEN_LABEL in repr(ctx)
