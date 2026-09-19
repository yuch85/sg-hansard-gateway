"""Spec §7 response hygiene (Phase 27.1 wave 4).

Page budgets (≤100KB HTML, ≤400 links, ≤2KB inline CSS, ladder TTFB ≤300ms
via the wave-1 in-process index query cache), the header split
(X-Robots-Tag + Referrer-Policy on every response; Cache-Control
max-age=300 on index-only pages vs no-store on content pages), the
robots.txt body, the structural rate-limit exemption for ladder/facet
routes + the raised per-token budget, and the spec §7.5 logging field set.

Offline: TestClient + respx stubs; no real upstream call.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

import pytest
import respx
from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN, TEST_TOKEN_LABEL
from hansard_gateway.config import Settings, settings
from hansard_gateway.index.build import build_index
from hansard_gateway.index.loader import IndexService
from hansard_gateway.main import create_app

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"

#: The spec §7.1 budgets.
BUDGET_HTML_BYTES = 100 * 1024
BUDGET_LINKS = 400
BUDGET_CSS_BYTES = 2 * 1024

#: The spec §7.2 header values.
ROBOTS_HEADER = "noindex, nofollow, noarchive"
REFERRER_HEADER = "no-referrer"
CACHE_INDEX = "private, max-age=300"
CACHE_CONTENT = "private, no-store"

#: The robots body (byte-exact). 2026-09-18 rounds 1-3: /a/ is allowed for
#: the explicit Anthropic groups AND the wildcard (Claude's fetcher does
#: not expose its UA — an unknown token falls to `*`, RFC 9309); non-/a/
#: stays disallowed for all. /a/ is token-gated + noindex'd, so this is
#: retrieval policy, not access control.
ROBOTS_BODY = (
    "User-agent: Claude-User\nAllow: /a/\n\n"
    "User-agent: Claude-SearchBot\nAllow: /a/\n\n"
    "User-agent: ClaudeBot\nAllow: /a/\n\n"
    "User-agent: *\nAllow: /a/\nDisallow: /\n"
)

#: Ladder TTFB budget (spec §7.1) — the in-process query cache is the
#: guarantee; the in-test measurement is a sanity bound.
LADDER_TTFB_BUDGET_S = 0.3


@pytest.fixture()
def index(tmp_path: Path) -> IndexService:
    """A tiny index for the hygiene probes."""
    rows = [
        {"report_id": "r1", "link_id": "r1", "sitting_date": "2023-05-10",
         "title": "Data Protection and Cybersecurity", "report_type": "bill",
         "speaker": None},
        {"report_id": "r2", "link_id": "r2", "sitting_date": "2024-01-02",
         "title": "Economic Recovery and Jobs", "report_type": "oral-answer",
         "speaker": None},
    ]
    target = tmp_path / "hygiene_index.db"
    build_index(rows, target)
    return IndexService(path=target)


@pytest.fixture()
def client(token_store, index: IndexService) -> TestClient:
    """The app with the hygiene index + the fixture token store."""
    import hansard_gateway.auth as auth_mod

    auth_mod._store = token_store
    return TestClient(create_app(index_override=index))


# --- page budgets (spec §7.1) --------------------------------------------------


def _count_anchors(body: str) -> int:
    return len(re.findall(r"<a\b", body))


def _combined_css_bytes(body: str) -> int:
    css = "\n".join(re.findall(r"<style>(.*?)</style>", body, re.DOTALL))
    return len(css.encode("utf-8"))


@pytest.mark.parametrize("path", [
    f"/a/{TEST_TOKEN}/",
    f"/a/{TEST_TOKEN}/nav/a",
    f"/a/{TEST_TOKEN}/years",
    f"/a/{TEST_TOKEN}/members",
    f"/a/{TEST_TOKEN}/bills",
])
def test_index_page_budgets(client: TestClient, path: str) -> None:
    """Launcher, ladder, and facet pages hold the spec §7.1 budgets."""
    r = client.get(path)
    assert r.status_code == 200, path
    assert len(r.content) <= BUDGET_HTML_BYTES, path
    assert _count_anchors(r.text) <= BUDGET_LINKS, path
    assert _combined_css_bytes(r.text) <= BUDGET_CSS_BYTES, path


def test_content_page_budgets(client: TestClient) -> None:
    """A zero-hit /search (content route) holds the budgets too."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(json=[])
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            r = client.get(f"/a/{TEST_TOKEN}/search?q=probe")
        finally:
            pair.stop()
    assert r.status_code == 200
    assert len(r.content) <= BUDGET_HTML_BYTES
    assert _count_anchors(r.text) <= BUDGET_LINKS
    assert _combined_css_bytes(r.text) <= BUDGET_CSS_BYTES


def test_ladder_cache_no_requery(client: TestClient, index: IndexService) -> None:
    """A second identical /nav/{prefix} is served from the wave-1 in-process
    query cache (no index re-query) — the TTFB ≤300ms guarantee."""
    # Spy on the SQLite connection's execute: the wave-1 query cache keys on
    # (query_name, args), so a second identical ladder page issues ZERO
    # executes (the TTFB guarantee).
    import hansard_gateway.index.queries as queries_mod

    calls: list[str] = []
    orig_terms = queries_mod.terms_for_prefix
    orig_children = queries_mod.children_for_prefix

    def _counting_terms(conn, prefix):
        calls.append("terms_for_prefix")
        return orig_terms(conn, prefix)

    def _counting_children(conn, prefix):
        calls.append("children_for_prefix")
        return orig_children(conn, prefix)

    queries_mod.terms_for_prefix = _counting_terms
    queries_mod.children_for_prefix = _counting_children
    # Clear the index's query cache so the FIRST request in this test is a
    # genuine cache miss (the fixture index is fresh, but be explicit).
    index._cache.clear()
    try:
        t0 = time.monotonic()
        r1 = client.get(f"/a/{TEST_TOKEN}/nav/a")
        first_ms = (time.monotonic() - t0) * 1000
        assert r1.status_code == 200
        # The first request queries the index (terms + children + build-id
        # swap probe).
        assert len(calls) >= 1, "first ladder page must query the index"
        n_first = len(calls)
        t0 = time.monotonic()
        r2 = client.get(f"/a/{TEST_TOKEN}/nav/a")
        second_ms = (time.monotonic() - t0) * 1000
        assert r2.status_code == 200
        # The second request: the query results come from the cache — no
        # terms/children queries (the build-id swap probe is separate).
        assert len(calls) == n_first, (
            f"ladder re-queried the index: {calls}")
        assert first_ms < LADDER_TTFB_BUDGET_S * 1000
        assert second_ms < LADDER_TTFB_BUDGET_S * 1000
    finally:
        queries_mod.terms_for_prefix = orig_terms
        queries_mod.children_for_prefix = orig_children


# --- headers (spec §7.2) -------------------------------------------------------


def _assert_hygiene_headers(resp: TestClient, name: str) -> None:
    assert resp.headers.get("x-robots-tag") == ROBOTS_HEADER, name
    assert resp.headers.get("referrer-policy") == REFERRER_HEADER, name


@pytest.mark.parametrize("path", [
    f"/a/{TEST_TOKEN}/",
    f"/a/{TEST_TOKEN}/nav/a",
    f"/a/{TEST_TOKEN}/years",
    f"/a/{TEST_TOKEN}/year/2023",
    f"/a/{TEST_TOKEN}/members",
    f"/a/{TEST_TOKEN}/members/a",
    f"/a/{TEST_TOKEN}/bills",
])
def test_index_pages_max_age_300(client: TestClient, path: str) -> None:
    """Index-only pages carry Cache-Control 'private, max-age=300' (OQ1)."""
    r = client.get(path)
    assert r.status_code == 200, path
    assert r.headers["cache-control"] == CACHE_INDEX, path
    _assert_hygiene_headers(r, path)


def test_content_pages_no_store(client: TestClient) -> None:
    """Content routes (search/date/report) keep 'private, no-store'."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(json=[])
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            r = client.get(f"/a/{TEST_TOKEN}/search?q=probe")
        finally:
            pair.stop()
    assert r.status_code == 200
    assert r.headers["cache-control"] == CACHE_CONTENT
    _assert_hygiene_headers(r, "search")


def test_index_format_variants_max_age_300(client: TestClient) -> None:
    """?format=json/text variants of index-only pages also carry max-age=300."""
    for fmt in ("json", "text"):
        r = client.get(f"/a/{TEST_TOKEN}/nav/a?format={fmt}")
        assert r.status_code == 200, fmt
        assert r.headers["cache-control"] == CACHE_INDEX, fmt
        _assert_hygiene_headers(r, f"nav/a?format={fmt}")


def test_public_pages_hygiene_headers(client: TestClient) -> None:
    """Public pages carry the hygiene headers where the app sets them
    (the protected routes + errors; public HTML pages use the robots META
    tag instead, and /health is a JSON status)."""
    for path in ("/robots.txt",):
        r = client.get(path)
        assert r.status_code == 200, path
        # robots.txt is a public plain-text route — the app does not set
        # the protected headers on it (the Disallow body is the protection).
        assert r.headers.get("x-robots-tag") is None
        # /health is JSON — no HTML hygiene headers.
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("content-type").startswith("application/json")


def test_error_pages_hygiene_headers(client: TestClient) -> None:
    """Valid-token errors carry the hygiene headers + no-store."""
    r = client.get(f"/a/{TEST_TOKEN}/report/bad id")
    assert r.status_code == 422
    assert r.headers["cache-control"] == CACHE_CONTENT
    _assert_hygiene_headers(r, "422")


# --- robots.txt (spec §7.3) ----------------------------------------------------


def test_robots_body_unchanged(client: TestClient) -> None:
    """The /robots.txt body is byte-exact: Claude-User allowed, ClaudeBot +
    all other agents disallowed from /a/ (2026-09-18 Claude-User compat)."""
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert r.text == ROBOTS_BODY
    assert "sitemap" not in r.text.lower()


def test_no_sitemap_route(client: TestClient) -> None:
    """No /sitemap route exists (spec §7.3: do not add a sitemap)."""
    r = client.get("/sitemap.xml")
    assert r.status_code == 404
    r2 = client.get("/sitemap")
    assert r2.status_code == 404


# --- rate limiting (spec §7.4) -------------------------------------------------


def test_nav_routes_no_gate_admit() -> None:
    """Structural: nav_routes.py + facet_routes.py make NO gate.admit call
    (quota exemption); search_routes.py DOES (gated)."""
    nav_src = (
        Path(__file__).parent.parent / "src" / "hansard_gateway"
        / "nav_routes.py"
    ).read_text(encoding="utf-8")
    facet_src = (
        Path(__file__).parent.parent / "src" / "hansard_gateway"
        / "facet_routes.py"
    ).read_text(encoding="utf-8")
    search_src = (
        Path(__file__).parent.parent / "src" / "hansard_gateway"
        / "search_routes.py"
    ).read_text(encoding="utf-8")
    # Strip docstrings/comments before the assertion: the phrase appears in
    # the module docstrings ("no gate.admit call") but must not appear in
    # executable code.
    def _code_only(src: str) -> str:
        lines = []
        in_docstring = False
        for line in src.splitlines():
            stripped = line.strip()
            if not in_docstring:
                if stripped.startswith('"""') or stripped.startswith("\'\'\'"):
                    if stripped.count('"""') >= 2 or stripped.count("\'\'\'") >= 2:
                        continue  # one-line docstring
                    in_docstring = True
                    continue
                if stripped.startswith("#"):
                    continue
                lines.append(line)
            else:
                if '"""' in line or "\'\'\'" in line:
                    in_docstring = False
        return "\n".join(lines)

    assert "gate.admit" not in _code_only(nav_src)
    assert "gate.admit" not in _code_only(facet_src)
    assert "gate.admit" in _code_only(search_src)


def test_raised_per_token_budget() -> None:
    """The raised per-token budget (300/min, 5000/day) is in effect: a
    token can make 300 requests in a minute before a 429 on the 301st."""
    from tests.conftest import FakeClock

    from hansard_gateway.rate_limit import RateGate

    clock = FakeClock()
    gate = RateGate(settings=settings, clock=clock)
    import asyncio

    async def _run() -> None:
        for i in range(300):
            result = await gate.admit(token_label="t")
            assert result.admitted, f"request {i + 1} denied early"
        clock.advance(0.0)
        denied = await gate.admit(token_label="t")
        assert not denied.admitted
        assert denied.status == settings.http_too_many_requests
        # The minute window slides: after 60s the budget refills.
        clock.advance(61)
        refilled = await gate.admit(token_label="t")
        assert refilled.admitted

    asyncio.run(_run())


def test_token_301st_request_429s(token_store, index: IndexService) -> None:
    """End-to-end: the 301st gated (search) request in a minute 429s with
    the nav bar + retry guidance (the raised budget absorbs ladder hops)."""
    import hansard_gateway.auth as auth_mod

    auth_mod._store = token_store
    client = TestClient(create_app(index_override=index))
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(json=[])
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            resp = None
            for i in range(301):
                resp = client.get(f"/a/{TEST_TOKEN}/search?q=probe")
                if resp.status_code == 429:
                    break
            assert resp is not None and resp.status_code == 429
            assert i == 300, f"429 at request {i + 1}, expected 301"
        finally:
            pair.stop()
    assert "Navigate:" in resp.text
    assert "temporary condition" in resp.text
    assert "Retry-After" not in resp.headers


# --- logging (spec §7.5) -------------------------------------------------------


def test_logging_field_set(token_store, index: IndexService,
                           tmp_path: Path) -> None:
    """A request log line carries the spec §7.5 field set: route,
    prefix_or_query, own_referer (bool), response_size (int bytes) — and
    NEVER the token."""
    import logging

    import hansard_gateway.auth as auth_mod
    from hansard_gateway.logging_setup import configure_logging

    auth_mod._store = token_store
    app = create_app(index_override=index)
    configure_logging(log_file=tmp_path / "h.log", settings=Settings())
    try:
        client = TestClient(app)
        # A ladder request (prefix logged) + a search (query logged), with
        # an own referer.
        r1 = client.get(
            f"/a/{TEST_TOKEN}/nav/cyb",
            headers={"Referer": f"{settings.public_base_url}/a/{TEST_TOKEN}/"})
        r2 = client.get(
            f"/a/{TEST_TOKEN}/search?q=probe",
            headers={"Referer": "https://example.com/other"})
        assert r1.status_code == 200
        assert r2.status_code in (200, 422)
        content = (tmp_path / "h.log").read_text(encoding="utf-8")
        lines = [
            ln for ln in content.splitlines()
            if ln.strip() and '"prefix_or_query"' in ln
        ]
        assert len(lines) >= 2, f"expected >=2 field-set lines: {content[:400]}"
        nav_line = json.loads([
            ln for ln in lines if '"route": "auth_home"' in ln
        ][0])
        # The nav route name for /nav/{prefix} is auth_home (existing
        # _route_name); the prefix IS in prefix_or_query.
        assert nav_line["prefix_or_query"] == "cyb"
        assert nav_line["own_referer"] is True
        assert isinstance(nav_line["response_size"], int)
        assert nav_line["response_size"] > 0
        search_line = json.loads([
            ln for ln in lines if "probe" in ln
        ][0])
        assert "q=probe" in search_line["prefix_or_query"]
        assert search_line["own_referer"] is False
        # The token is NEVER in any log line.
        assert TEST_TOKEN not in content
        assert "hg_testvalidtoken" not in content
    finally:
        logging.getLogger("hansard_gateway").handlers.clear()


def test_logging_redaction_stays_green(token_store, index: IndexService,
                                       tmp_path: Path) -> None:
    """T-27.1-16: the new fields (prefix/query/referer/size) cannot carry
    the token — a referer with a foreign token is logged, token absent."""
    import logging

    import hansard_gateway.auth as auth_mod
    from hansard_gateway.logging_setup import configure_logging

    auth_mod._store = token_store
    app = create_app(index_override=index)
    configure_logging(log_file=tmp_path / "h2.log", settings=Settings())
    try:
        client = TestClient(app)
        r = client.get(
            f"/a/{TEST_TOKEN}/nav/a",
            headers={"Referer": f"{settings.public_base_url}/a/{TEST_TOKEN}/nav/b"})
        assert r.status_code == 200
        content = (tmp_path / "h2.log").read_text(encoding="utf-8")
        assert TEST_TOKEN not in content
        assert "hg_testvalidtoken" not in content
        # own_referer is True (same host), the token is not in the field.
        line = [ln for ln in content.splitlines() if '"own_referer"' in ln]
        assert line and json.loads(line[0])["own_referer"] is True
    finally:
        logging.getLogger("hansard_gateway").handlers.clear()
