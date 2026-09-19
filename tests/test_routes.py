"""Tracer route tests: public routes, the E2E /report path, security headers,
upstream-failure fail-closed behaviour, and invalid-ID 422 (TestClient + respx).

Offline — the upstream is stubbed with respx; no real SPRS call is made.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

import hansard_gateway.auth as auth_mod
from hansard_gateway.auth import TEST_TOKEN, TEST_TOKEN_LABEL
from hansard_gateway.main import create_app

#: The E2E target report (spec §30-A).
E2E_REPORT_ID = "037_20041019_S0004_T0023"

#: Upstream base the respx mock intercepts (matches SprsClient's base_url).
UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"


@pytest.fixture()
def app(token_store):
    """The full app wired to the fixture token store."""
    auth_mod._store = token_store
    return create_app()


@pytest.fixture()
def client(app) -> TestClient:
    """A TestClient over the app."""
    return TestClient(app)


def _saf_fixture(fixtures_dir: Path) -> dict[str, object]:
    """Load the 2004 SAF sprs2 topic payload (the tracer's E2E target)."""
    path = fixtures_dir / "topic_20041019_saf.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_public_routes_200(client: TestClient) -> None:
    """/, /about, /health, /robots.txt all 200; health is JSON; robots allows /a/
    (2026-09-18 Claude compat — token-gated + noindex'd, see test_hygiene)."""
    for route in ("/", "/about", "/health", "/robots.txt"):
        resp = client.get(route)
        assert resp.status_code == 200, route
    assert client.get("/health").json() == {"status": "ok"}
    assert "Allow: /a/" in client.get("/robots.txt").text


def test_report_tracer_e2e(client: TestClient, fixtures_dir: Path) -> None:
    """GET /a/{token}/report/{id} → 200 with the full transcript in raw bytes."""
    fixture = _saf_fixture(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(
            json={"resultHTML": fixture}
        )
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
        assert resp.status_code == 200
        body = resp.text
        assert "Singapore Armed Forces" in body
        assert "Transcript SHA-256" in body
        assert "Singapore Parliamentary Reports" in body
        # The transcript is in the static body — no <script> gate.
        assert "<script" not in body.lower()


def test_report_flat_payload_e2e(client: TestClient, fixtures_dir: Path) -> None:
    """Flat (unwrapped) getHansardTopic payload → 200 with the full transcript.

    Regression: the live 2004 upstream returns the sprs2 payload flat at the
    top level (``htmlContent`` present, NO ``resultHTML`` wrapper) — the exact
    shape the committed fixture has. The client must tolerate it instead of
    raising ``UpstreamError: resultHTML missing`` (the 502 defect).
    """
    fixture = _saf_fixture(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=fixture)
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
        assert resp.status_code == 200
        body = resp.text
        # The sprs2 MP_NAME-comment speakers render into the page text.
        assert "Dr Ong Chit Chung" in body
        # The full transcript + provenance footer are present.
        assert "Singapore Armed Forces" in body
        assert "Transcript SHA-256" in body


def test_report_protected_headers(client: TestClient, fixtures_dir: Path) -> None:
    """The protected 200 carries no-store / no-referrer / noindex headers."""
    fixture = _saf_fixture(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json={"resultHTML": fixture})
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert resp.status_code == 200
    assert "no-store" in resp.headers.get("cache-control", "")
    assert resp.headers.get("referrer-policy") == "no-referrer"
    assert "noindex" in resp.headers.get("x-robots-tag", "")


def test_upstream_unavailable(client: TestClient) -> None:
    """Upstream timeout → 502/503/504 + static error page, no fabricated text."""
    import httpx

    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").mock(
            side_effect=httpx.ReadTimeout("read timed out")
        )
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert resp.status_code in (502, 503, 504)
    body = resp.text
    assert "No transcript has been generated or substituted" in body
    # No fabricated transcript: the report title must NOT appear.
    assert "Singapore Armed Forces (Amendment" not in body


def test_invalid_report_id(client: TestClient) -> None:
    """A traversal id is rejected (422/404) with zero upstream POSTs."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        route = mock.post("/getHansardTopic")
        # FastAPI's path converter rejects '/' before the handler, so a
        # traversal id is refused at the router; either way no upstream call.
        resp = client.get(f"/a/{TEST_TOKEN}/report/..%2f..%2fetc%2fpasswd")
        assert resp.status_code in (404, 422)
        assert route.call_count == 0


def test_no_unauthenticated_report_route(client: TestClient) -> None:
    """/report/{id} without the /a/{token} prefix → 404 (no bypass)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        route = mock.post("/getHansardTopic")
        resp = client.get(f"/report/{E2E_REPORT_ID}")
    assert resp.status_code == 404
    assert route.call_count == 0


def test_authenticated_home_token_preserving(client: TestClient) -> None:
    """GET /a/{token}/ → 200 and links preserve the /a/{token}/ prefix.

    Phase 27.1 wave 2: the home is now the form-free departure board — its
    links are /nav/{c}, /search?q=..., /date/..., /years, /members, /bills.
    """
    resp = client.get(f"/a/{TEST_TOKEN}/")
    assert resp.status_code == 200
    assert f"/a/{TEST_TOKEN}/nav/" in resp.text
    # With no index built the launcher degrades to empty sections; the token
    # is preserved in every rendered link (nav links + the format sibling).
    assert f"/a/{TEST_TOKEN}/nav/a" in resp.text
    assert f"/a/{TEST_TOKEN}/?format=json" in resp.text


PAIR_BASE = "https://search.pair.gov.sg"


def _search_fixture_rows(fixtures_dir: Path) -> list[dict]:
    """Load the 2004 searchResult fixture rows."""
    import json as _json

    return _json.loads(
        (fixtures_dir / "searchresult_20041019_p1.json").read_text(encoding="utf-8")
    )


def test_search_date_protected_headers(client: TestClient, fixtures_dir: Path) -> None:
    """/search + /date 200s carry no-store / no-referrer / noindex (addendum §11-13)."""
    rows = _search_fixture_rows(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(json=rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False)
        pair.start()
        try:
            search = client.get(f"/a/{TEST_TOKEN}/search?q=Pension%20Fund")
            date_resp = client.get(f"/a/{TEST_TOKEN}/date/2004-10-19")
        finally:
            pair.stop()
    for name, resp in (("search", search), ("date", date_resp)):
        assert resp.status_code == 200, name
        assert "no-store" in resp.headers.get("cache-control", ""), name
        assert resp.headers.get("referrer-policy") == "no-referrer", name
        assert "noindex" in resp.headers.get("x-robots-tag", ""), name
