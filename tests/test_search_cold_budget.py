"""27.1-search-hop-rectify: cold /search first-page page budget.

The diagnosis (logs/phase-27.1-search-hop-diagnosis.md) established that a
COLD search cache for a large-result query runs the full SPRS sweep
(1,013 hits -> 51 POSTs ~= 16s; 2,505 -> 126 POSTs ~= 34s), while the
free-tier LLM web tools that can only click rendered links abort at ~5s.
The rectify bounds the COLD sweep to ``settings.search_cold_page_budget``
sweep pages when the probed maxResult exceeds the budget, returns the
collected rows as a normal 200 page with the F-4 honest header, and caches
the partial result under the SAME search key so clicked pagination answers
fast and never re-sweeps.

Offline: SPRS + Pair are stubbed with respx; the HIB index comes from the
spec 8.1 fixture corpus (``client_with_index`` carries b1/b2/b3 under
'Health Information Bill').
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import Settings, settings

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"

#: The spec 8.1 corpus's HIB report (pinned to the top by the F-4 boost).
#: NOTE: the boost pins the reports of the exact indexed term — in the
#: ``client_with_index`` corpus that is hib1 ('Health Information Bill'),
#: which the stubbed upstream corpus does NOT contain, so the pinned id is
#: corpus-dependent. The tests assert the boost by ORDER (pinned id before
#: every upstream row), not by a specific id.
_HIB_REPORT_ID = "b1"

#: The exact indexed HIB term the boost scenario queries (client-encoded).
_HIB_QUERY = "Health%20Information%20Bill"


def _row(report_id: str, title: str, *, max_result: str) -> dict[str, Any]:
    """One searchResult row in the committed fixture shape (sprs2 date form)."""
    return {
        "reportId": report_id,
        "htmlFileName": report_id,
        "title": title,
        "sittingDate": "12-01-2026",
        "reportVersion": "sprs2",
        "reportType": "bill",
        "mpNames": None,
        "maxResult": max_result,
    }


def _corpus(n: int, *, max_result: int) -> list[dict[str, Any]]:
    """``n`` distinct upstream rows (maxResult says ``max_result`` total)."""
    return [
        _row(f"r{i:04d}", f"Matter number {i}", max_result=str(max_result))
        for i in range(n)
    ]


def _purge_cache(client: TestClient, query: str) -> None:
    """Drop any cached page for the exact query (fresh-cold per test)."""
    from urllib.parse import unquote

    cache = client.app.state.cache
    key = cache.search_key(
        keyword=unquote(query),
        date_from="", date_to="",
        limit=settings.search_max_limit)
    # Drop both the sweep entry and every per-page memo for it.
    cache._cache.pop(key, None)
    for k in [k for k in cache._cache if k.startswith(key + ":page:")]:
        cache._cache.pop(k, None)


def _stub_sprs(mock: respx.MockRouter, rows: list[dict[str, Any]]) -> None:
    """Stub searchResult, slicing ``rows`` by (startIndex, endIndex) — the
    same pattern the spec 8.1 strict simulator uses (a chained ``.respond()``
    does not advance per sweep call)."""

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        start = int(body.get("startIndex") or 0)
        end = int(body.get("endIndex") or start)
        return httpx.Response(200, json=rows[start : end + 1])

    mock.post("/searchResult").mock(side_effect=_handler)


def _search(
    client: TestClient,
    *,
    query: str,
    rows: list[dict[str, Any]],
    path: str = "",
) -> httpx.Response:
    """One /search GET over a respx-stubbed upstream (Pair = no hits)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            return client.get(f"/a/{TEST_TOKEN}/search?q={query}{path}")
        finally:
            pair.stop()


# --- (1) cold large query: bounded POSTs + honest header + boost ------------


def test_cold_large_query_bounded_post_count(client_with_index: TestClient) -> None:
    """A 1,000-hit cold query costs at most probes+budget+1 upstream POSTs —
    NOT the ~51 of the full sweep (the 16s that blew the ~5s tool budget)."""
    s = Settings()
    rows = _corpus(1000, max_result=1000)
    _purge_cache(client_with_index, _HIB_QUERY)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            resp = client_with_index.get(f"/a/{TEST_TOKEN}/search?q={_HIB_QUERY}")
        finally:
            pair.stop()
    route = mock.post("/searchResult")
    bound = s.search_max_probes + s.search_cold_page_budget + 1
    assert resp.status_code == 200
    assert route.call_count <= bound, (
        f"{route.call_count} upstream POSTs > bound {bound} "
        f"(probes={s.search_max_probes}, budget={s.search_cold_page_budget})"
    )
    # The live failure mode is ~51 POSTs for this shape — the bound is far
    # tighter: assert the sweep really stopped early.
    full_sweep_calls = 1000 // s.search_page_size + s.search_max_no_gain + 1
    assert route.call_count < full_sweep_calls
    # The F-4 honest header: the rendered slice of the ~1,000 estimate
    # (limit 50 upstream + the boost-pinned index reports, if any).
    assert " of ~1,000 estimated results" in resp.text, resp.text[:600]


def test_cold_truncated_page_honest_header_and_boost(
    client_with_index: TestClient,
) -> None:
    """Page 1 of a truncated cold sweep: the F-4 honest header reflects the
    TRUNCATED collected count, the exact-term boost still pins b1 on top, and
    the rendered ?page=2 continuation link is present (click-only invariant)."""
    s = Settings()
    rows = _corpus(1000, max_result=1000)
    _purge_cache(client_with_index, _HIB_QUERY)
    resp = _search(client_with_index, query=_HIB_QUERY, rows=rows)
    assert resp.status_code == 200
    body = resp.text
    # Honest F-4 header: the rendered slice (limit upstream rows + the
    # boost-pinned index reports) of the ~1,000 estimate.
    assert "Showing" in body and " of ~1,000 estimated results" in body
    # Honest truncated-sweep note: the COLLECTED count (budget*page_size),
    # never the bare estimate.
    collected = s.search_cold_page_budget * s.search_page_size
    assert f"first {collected:,} of ~{1000:,} estimated" in body
    # F-4 boost survives the truncated row set: the index-pinned HIB report
    # leads the page, above every upstream row (page 1 = b1 + r0000-r00049,
    # the first 50 of the 100 collected).
    first_report = body.index('/report/')
    first_upstream = body.index("/report/r0000")
    assert first_report < first_upstream, (
        "the boost-pinned report must lead the page, above upstream rows"
    )
    assert "pinned to the top" in body
    # Pagination stays click-only: a rendered absolute ?page=2 link.
    assert f"{settings.public_base_url.rstrip('/')}/a/{TEST_TOKEN}/search?" in body
    assert "page=2" in body
    # JSON carries the honest fields: rendered_total = collected rows,
    # total = probed estimate.
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            json_resp = client_with_index.get(
                f"/a/{TEST_TOKEN}/search?q={_HIB_QUERY}&format=json")
        finally:
            pair.stop()
    data = json_resp.json()
    assert data["rendered_total"] == collected
    assert data["total"] == 1000


def test_cold_truncated_page2_answers_from_cache(client_with_index: TestClient) -> None:
    """After a truncated cold page 1, the clicked ?page=2 is a CACHE HIT —
    zero new upstream POSTs (the partial sweep is cached under the same key)."""
    s = Settings()
    rows = _corpus(1000, max_result=1000)
    _purge_cache(client_with_index, _HIB_QUERY)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            first = client_with_index.get(f"/a/{TEST_TOKEN}/search?q={_HIB_QUERY}")
            calls_page1 = mock.post("/searchResult").call_count
            page2 = client_with_index.get(
                f"/a/{TEST_TOKEN}/search?q={_HIB_QUERY}&page=2")
            calls_page2 = mock.post("/searchResult").call_count
        finally:
            pair.stop()
    assert first.status_code == 200
    assert 0 < calls_page1 <= s.search_max_probes + s.search_cold_page_budget + 1
    assert page2.status_code == 200
    assert calls_page2 == calls_page1, (
        "page 2 of the collected rows must be served from the cache — "
        f"no new sweep (page1={calls_page1}, page2={calls_page2})"
    )
    # Page 2 renders the next slice of the collected rows (r0050-r00099)
    # plus the boost-pinned index report, and stays honest about the
    # estimate.
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            page2_json = client_with_index.get(
                f"/a/{TEST_TOKEN}/search?q={_HIB_QUERY}&page=2&format=json")
        finally:
            pair.stop()
    data = page2_json.json()
    assert data["rendered_total"] == s.search_cold_page_budget * s.search_page_size
    # Boost still applies on the continuation page: the pinned b1 leads.
    assert data["hits"][0]["link_id"] == _HIB_REPORT_ID
    assert "r0050" in [h["link_id"] for h in data["hits"]]
    assert " of ~1,000 estimated results" in page2.text


def test_cold_page_beyond_collected_rows_honest_end(
    client_with_index: TestClient,
) -> None:
    """A continuation page past the collected rows: 200, an honest
    'end of the collected results' note, and a rendered link BACK to page 1
    (never a re-sweep — pagination stays click-only)."""
    s = Settings()
    rows = _corpus(1000, max_result=1000)
    _purge_cache(client_with_index, _HIB_QUERY)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            client_with_index.get(f"/a/{TEST_TOKEN}/search?q={_HIB_QUERY}")
            calls_page1 = mock.post("/searchResult").call_count
            page3 = client_with_index.get(
                f"/a/{TEST_TOKEN}/search?q={_HIB_QUERY}&page=3")
            calls_page3 = mock.post("/searchResult").call_count
        finally:
            pair.stop()
    assert page3.status_code == 200
    assert calls_page3 == calls_page1, "past-end page must not re-sweep"
    assert "End of the collected results" in page3.text
    # The back-to-page-1 link is a RENDERED finished URL (no ?page= param).
    back = f"{settings.public_base_url.rstrip('/')}/a/{TEST_TOKEN}/search?q={_HIB_QUERY}"
    assert back in page3.text


# --- (2) small query: sweeps to completion, unchanged ------------------------


def test_small_query_sweeps_to_completion(client_with_index: TestClient) -> None:
    """A query whose probed total fits in the budget sweeps ALL of it: full
    POST count, no truncation note, and the warm second request is a cache
    hit with zero new POSTs (existing behaviour, unchanged)."""
    s = Settings()
    small = 3 * s.search_page_size  # 60 < budget*page_size (100)
    rows = _corpus(small, max_result=small)
    # The fixture index pins 1 report for the exact HIB term; it is not in
    # the stubbed 60-row upstream corpus, so page 1 renders 60 + 1.
    pinned = 1
    _purge_cache(client_with_index, _HIB_QUERY)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            first = client_with_index.get(f"/a/{TEST_TOKEN}/search?q={_HIB_QUERY}")
            calls_cold = mock.post("/searchResult").call_count
            warm = client_with_index.get(f"/a/{TEST_TOKEN}/search?q={_HIB_QUERY}")
            calls_warm = mock.post("/searchResult").call_count
        finally:
            pair.stop()
    assert first.status_code == 200
    # The full sweep: all 60 rows collected (3 full pages) + the no-gain
    # pages that prove the sweep reached the end — every row was fetched.
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_sprs(mock, rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            small_json = client_with_index.get(
                f"/a/{TEST_TOKEN}/search?q={_HIB_QUERY}&format=json")
        finally:
            pair.stop()
    data = small_json.json()
    # The sweep collected ALL 60 rows (rendered_total = total = 60) — the
    # query fits within the budget so it swept to completion. Page 1 renders
    # the first `limit` (50) of the 60 collected + the boost-pinned b1 = 51.
    assert data["rendered_total"] == small
    assert data["total"] == small
    assert len(data["hits"]) == s.search_max_limit + pinned
    assert data["hits"][0]["link_id"] == _HIB_REPORT_ID  # b1 pinned first
    # 60 rows / 20 per page = 3 fetches; the sweep stops when offset (60) >=
    # total (60), so no no-gain tail (the existing D-06 termination).
    assert calls_cold == small // s.search_page_size
    assert "stopped after the first page budget" not in first.text
    # Warm within the 600s TTL: a cache hit, no new upstream POST.
    assert warm.status_code == 200
    assert calls_warm == calls_cold
    # Warm page 1 is byte-stable across requests (same cache entry).
    assert warm.text == first.text


# --- (3) provider unit: the sweep cap itself ---------------------------------


async def test_sweep_cap_at_budget_unit() -> None:
    """SprsSearchProvider.search(cold_budget=N) stops after N pages when the
    probed max exceeds N*page_size; with cold_budget=None it sweeps to
    completion (the /date TOC and warm-rebuild path)."""
    s = Settings()
    page_size = s.search_page_size
    rows = _corpus(1000, max_result=1000)

    class _StubClient:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows
            self.calls = 0

        async def search_result(self, **kwargs: Any) -> list[dict[str, Any]]:
            start = int(kwargs["start_index"])
            end = int(kwargs["end_index"])
            self.calls += 1
            return self._rows[start : end + 1]

    from hansard_gateway.search.sprs import SprsSearchProvider

    async def run(budget: Any) -> tuple[int, int, int]:
        client = _StubClient(rows)
        provider = SprsSearchProvider(settings=s, client=client)  # type: ignore[arg-type]
        page = await provider.search(
            query="q", date_from=None, date_to=None, speaker=None,
            page=1, limit=50, cold_budget=budget)
        return client.calls, len(page.hits), page.rendered_total

    calls, hits, collected = await run(budget=s.search_cold_page_budget)
    assert calls == s.search_cold_page_budget
    assert hits == 50
    assert collected == s.search_cold_page_budget * page_size

    calls_full, _hits, collected_full = await run(None)
    assert collected_full == 1000
    assert calls_full > calls  # the unbounded sweep keeps going
