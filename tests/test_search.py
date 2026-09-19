"""Search provider + route tests: SPRS sweep/dedupe, /search, /date, formats.

Offline — the SPRS + Pair upstreams are stubbed with respx. The sweep tests
use overlapping pages (simulating the two disagreeing LB nodes) to prove the
dedupe loop continues past the first page and stops on no-gain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

import hansard_gateway.auth as auth_mod
from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import Settings
from hansard_gateway.main import create_app
from hansard_gateway.models import SearchHit, SearchPage
from hansard_gateway.rate_limit import RateGate
from hansard_gateway.search.sprs import SprsSearchProvider, normalize_row
from hansard_gateway.sprs.client import SprsClient
from tests.conftest import FakeClock

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"


def _row(report_id: str, *, html: str | None = None, date_str: str = "19-10-2004",
         title: str = "T", total: str = "3", version: str = "sprs2") -> dict:
    """One searchResult-shaped row for stub pages."""
    return {
        "reportId": report_id,
        "htmlFileName": html,
        "sittingDate": date_str,
        "title": title,
        "reportType": "bill",
        "mpNames": None,
        "maxResult": total,
        "reportVersion": version,
        "reportContent": None,
        "fromDay": None,
        "fromMonth": None,
        "fromYear": None,
    }


def _stub_sprs(mock: respx.MockRouter, pages: list[list[dict]]) -> respx.Route:
    """Stub searchResult so successive calls return the pages in order."""
    route = mock.post("/searchResult").respond(json=pages[0])
    for page in pages[1:]:
        route = route.respond(json=page)
    return route


@pytest.fixture()
def app(token_store):
    """The full app wired to the fixture token store."""
    auth_mod._store = token_store
    return create_app()


@pytest.fixture()
def client(app) -> TestClient:
    """A TestClient over the app."""
    return TestClient(app)


async def test_sprs_sweep_dedupe() -> None:
    """Overlapping duplicate pages dedupe to the unique count; no page-1 stop."""
    a, b, c = _row("ra#", title="A"), _row("rb#", title="B"), _row("rc#", title="C")
    pages = [
        [a, b, c],   # page 0
        [c, a, b],   # page 1 — all duplicates (LB node disagreement)
    ]
    settings = Settings()

    class _StubClient:
        def __init__(self, pages: list[list[dict]]) -> None:
            self._pages = pages
            self.calls = 0

        async def search_result(self, **_: object) -> list[dict]:
            idx = min(self.calls, len(self._pages) - 1)
            self.calls += 1
            return self._pages[idx]

    client = _StubClient(pages)  # type: ignore[assignment]
    provider = SprsSearchProvider(settings=settings, client=client)

    async def run() -> SearchPage:
        return await provider.search(
            query="x", date_from=None, date_to=None,
            speaker=None, page=1, limit=50,
        )

    page = await run()
    assert [h.title for h in page.hits] == ["A", "B", "C"]
    assert page.total == 3
    # Dedupe held across overlapping pages (the sum of page lengths is 6).
    assert len(page.hits) == 3
    assert client.calls <= len(pages) + settings.search_max_no_gain


def test_normalize_row_link_id_prefers_html() -> None:
    """link_id prefers htmlFileName else the stripped live reportId (D-02)."""
    with_html = normalize_row(
        {"reportId": "00004673-WA.00000605-WA_1#hansardContent-abc#",
         "htmlFileName": "003_20041019_S0002", "sittingDate": "19-10-2004",
         "title": "T", "reportType": "atbp", "reportVersion": "sprs2",
         "fromDay": "19", "fromMonth": "10", "fromYear": "2004", "mpNames": None,
         "reportContent": None}
    )
    assert with_html.link_id == "003_20041019_S0002"
    live = normalize_row(
        {"reportId": "bill-742#", "htmlFileName": None,
         "sittingDate": "8-1-2025", "title": "T", "reportType": "bill",
         "reportVersion": "sprs3", "fromDay": None, "fromMonth": None,
         "fromYear": None, "mpNames": None, "reportContent": None}
    )
    assert live.link_id == "bill-742"
    assert live.date.isoformat() == "2025-01-08"


def test_search_route_e2e(client: TestClient) -> None:
    """GET /a/{token}/search → 200 HTML; every hit links to /a/{token}/report/."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "searchresult_20041019_p1.json").read_text()
    )
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, [fixture])
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False)
        pair.start()
        try:
            resp = client.get(f"/a/{TEST_TOKEN}/search?q=Pension%20Fund")
        finally:
            pair.stop()
    assert resp.status_code == 200
    assert f"/a/{TEST_TOKEN}/report/" in resp.text
    assert "Hansard search results" in resp.text


def test_search_token_preserving(client: TestClient) -> None:
    """No result link is a bare /report/ (addendum §10)."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "searchresult_20041019_p1.json").read_text()
    )
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, [fixture])
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False)
        pair.start()
        try:
            resp = client.get(f"/a/{TEST_TOKEN}/search?q=Fund")
        finally:
            pair.stop()
    assert resp.status_code == 200
    assert 'href="/report/' not in resp.text
    base = Settings().public_base_url.rstrip("/")
    assert f'href="{base}/a/{TEST_TOKEN}/report/' in resp.text


def test_search_query_escaped(client: TestClient) -> None:
    """A <script> in q is displayed HTML-escaped (spec §30-E)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(json=[])
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False)
        pair.start()
        try:
            resp = client.get(f"/a/{TEST_TOKEN}/search?q={('<script>alert(1)</script>')}")
        finally:
            pair.stop()
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_search_limit_422(client: TestClient) -> None:
    """q of 301 chars → 422 (addendum §18)."""
    long_q = "a" * 301
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        route = mock.post("/searchResult")
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False)
        pair.start()
        try:
            resp = client.get(f"/a/{TEST_TOKEN}/search?q={long_q}")
        finally:
            pair.stop()
    assert resp.status_code == 422
    assert route.call_count == 0


def test_date_toc(client: TestClient) -> None:
    """/date returns a TOC; getHansardTopic is NEVER called (D-05)."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "searchresult_20041019_p1.json").read_text()
    )
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        search_route = _stub_sprs(mock, [fixture])
        topic_route = mock.post("/getHansardTopic")
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False)
        pair.start()
        try:
            resp = client.get(f"/a/{TEST_TOKEN}/date/2004-10-19")
        finally:
            pair.stop()
    assert resp.status_code == 200
    assert "Singapore Parliament" in resp.text
    assert f"/a/{TEST_TOKEN}/report/037_20041019_S0004_T0023" in resp.text
    assert topic_route.call_count == 0


def test_date_invalid_422(client: TestClient) -> None:
    """A malformed date → 422."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False):
        resp = client.get(f"/a/{TEST_TOKEN}/date/not-a-date")
    assert resp.status_code == 422


def test_json_format(client: TestClient) -> None:
    """format=json returns the normalized SearchPage as parseable JSON."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "searchresult_20041019_p1.json").read_text()
    )
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, [fixture])
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False)
        pair.start()
        try:
            resp = client.get(f"/a/{TEST_TOKEN}/search?q=Fund&format=json")
        finally:
            pair.stop()
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert data["provider"] == "sprs"
    assert isinstance(data["hits"], list) and data["hits"]
    assert data["hits"][0]["link_id"]


def test_search_security_headers(client: TestClient) -> None:
    """/search + /date 200s carry the protected header set (addendum §11-13)."""
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "searchresult_20041019_p1.json").read_text()
    )
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, [fixture])
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False)
        pair.start()
        try:
            search = client.get(f"/a/{TEST_TOKEN}/search?q=Fund")
            date_resp = client.get(f"/a/{TEST_TOKEN}/date/2004-10-19")
        finally:
            pair.stop()
    for resp in (search, date_resp):
        assert resp.status_code == 200
        assert "no-store" in resp.headers.get("cache-control", "")
        assert resp.headers.get("referrer-policy") == "no-referrer"
        assert "noindex" in resp.headers.get("x-robots-tag", "")
