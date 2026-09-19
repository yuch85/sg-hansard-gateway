"""F-4 (wave 6) rectify: exact-term boost, honest results header,
related-topics tie-break.

Offline: the HIB-style scenario is built with the spec 8.1 fixture terms
(the ``rich_index`` corpus carries 'Health Information Bill' + the 120-row
'Budget' corpus), and the upstream is stubbed with respx. No live call.
"""

from __future__ import annotations

import json
from typing import Any

import respx
from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import Settings, settings
from hansard_gateway.index.loader import IndexService
from hansard_gateway.models import SearchHit, SearchPage
from hansard_gateway.render.links import build_related_terms
from hansard_gateway.render import results_line
from hansard_gateway.search.boost import apply_exact_term_boost
from tests.conftest import RICH_HIT_COUNT, _rich_fixture_report_rows

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"

BASE = settings.public_base_url.rstrip("/")

#: The fixture index's HIB surface (the exact term for the boost scenario).
_HIB_SURFACE = "Health Information Bill"

#: The fixture index's second HIB word ('information' shares 'health').
_HIB_PARTIAL = "Health Information"


def _row(report_id: str, title: str, *, max_result: str) -> dict[str, Any]:
    """One searchResult row in the committed fixture shape (sprs3 date form)."""
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


def _hib_noise_rows() -> list[dict[str, Any]]:
    """Upstream rows for q='Health Information Bill' where the HIB reports
    rank on page 3 (the F-4 live symptom): 40 noise rows first (maxResult
    says ~2505), then the HIB reports (bill-773/774/775 @2026-01-12,
    bill-intro-668 — the live ids, absent from the fixture index)."""
    rows = [
        _row(f"noise{i:03d}", f"Health information and other matters {i}",
             max_result="2505")
        for i in range(40)
    ]
    rows += [
        _row("bill-773", "Health Information Bill", max_result="2505"),
        _row("bill-774", "Health Information Bill", max_result="2505"),
        _row("bill-775", "Health Information Bill", max_result="2505"),
    ]
    return rows


def _hib_search(client: TestClient, *, query: str, rows: list[dict[str, Any]]):
    """Run one /search with a respx stubbed upstream.

    The stub slices ``rows`` by (startIndex, endIndex) — the same pattern the
    spec 8.1 strict simulator uses (a chained ``.respond()`` does NOT advance
    per sweep call, which would silently return page 1 on every probe)."""
    import json as _json
    from urllib.parse import unquote

    # The ReportCache is per-app (create_app builds its own) but the
    # app_with_index fixture re-creates the app per test — still purge any
    # entry for this exact query to be safe (real-time 600s TTL).
    cache = client.app.state.cache
    key = cache.search_key(
        keyword=unquote(query),
        date_from="", date_to="", limit=settings.search_max_limit)
    # Drop the sweep entry and every per-page memo for it (27.1-search-hop-
    # rectify made the search key page-agnostic).
    cache._cache.pop(key, None)
    for k in [k for k in cache._cache if k.startswith(key + ":page:")]:
        cache._cache.pop(k, None)

    def _handler(request: object) -> object:
        body = _json.loads(request.content)
        start = int(body.get("startIndex") or 0)
        end = int(body.get("endIndex") or start)
        return __import__("httpx").Response(200, json=rows[start : end + 1])

    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").mock(side_effect=_handler)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            return client.get(f"/a/{TEST_TOKEN}/search?q={query}")
        finally:
            pair.stop()


# --- (1) exact-term boost ----------------------------------------------------


def test_exact_term_boost_pins_hib_to_top(
    client_with_index: TestClient,
) -> None:
    """q='Health Information Bill' (an exact indexed term): the fixture's HIB
    reports (b1/b2) are PINNED to the top of page 1 even though the stubbed
    upstream ranks the real HIB reports (bill-773/...) on page 3."""
    resp = _hib_search(
        client_with_index, query="Health%20Information%20Bill",
        rows=_hib_noise_rows(),
    )
    assert resp.status_code == 200
    body = resp.text
    # The index-pinned reports link first ...
    assert body.index(f"/report/b1") < body.index(f"/report/bill-773"), (
        "exact-term reports must be pinned above the upstream-ranked HIB "
        "reports (they were page 3 pre-fix)"
    )
    assert body.index(f"/report/b1") < body.index(f"/report/noise000")
    # b1 is the fixture index's report under the exact term
    # "health information bill". (b2 is a DISTINCT indexed term —
    # "health information bill amendment no 2" — and is not pinned here;
    # it is exercised by the unique-prefix test below.)
    # ... and the boost is announced (no silent re-ranking).
    assert "pinned to the top" in body
    # The header is the honest rendered count, not the bare ~2,505 estimate.
    assert "estimated results" in body
    assert "Results: 2505" not in body


def test_unique_prefix_boost_appends(
    client_with_index: TestClient,
) -> None:
    """q='Economic Recovery' is a strict word-prefix of exactly ONE indexed
    term ('Economic Recovery and Jobs') — its reports (t3/t4) are APPENDED
    (ranked boost, documented threshold: unique candidate only), after the
    upstream hits, and the boost is announced (not pinned)."""
    rows = [_row("noise000", "Economic recovery and other matters",
                 max_result="5")]
    resp = _hib_search(
        client_with_index,
        query="Economic%20Recovery",
        rows=rows,
    )
    assert resp.status_code == 200
    body = resp.text
    # The ranked boost APPENDS: the boosted reports link AFTER the upstream
    # hit (they are not pinned to the top).
    assert body.index(f"/report/t3") > body.index(f"/report/noise000"), (
        "prefix boost APPENDS (ranked), it does not pin"
    )
    assert body.index(f"/report/t4") > body.index(f"/report/noise000")
    assert "closest indexed term" in body
    assert "pinned to the top" not in body


def test_exact_term_amendment_pins(
    client_with_index: TestClient,
) -> None:
    """q='Health Information Bill (Amendment No. 2)' normalises (parens
    stripped) to an EXACT indexed term — its report b2 is PINNED to the top
    (exact-title mode), distinct from the ranked unique-prefix case above."""
    rows = [_row("noise000", "Health information and other matters",
                 max_result="5")]
    resp = _hib_search(
        client_with_index,
        query="Health%20Information%20Bill%20(Amendment%20No.%202)",
        rows=rows,
    )
    assert resp.status_code == 200
    body = resp.text
    assert body.index(f"/report/b2") < body.index(f"/report/noise000"), (
        "exact indexed term must PIN to the top"
    )
    assert "pinned to the top" in body


def test_ambiguous_prefix_no_boost(
    client_with_index: TestClient,
) -> None:
    """q='Health' prefixes MULTIPLE indexed terms — the boost is skipped
    (documented threshold), upstream order is untouched, no boost note."""
    rows = [_row("noise000", "Health information and other matters",
                 max_result="2")]
    resp = _hib_search(
        client_with_index, query="Health", rows=rows)
    assert resp.status_code == 200
    assert f"/report/b1" not in resp.text
    assert "pinned to the top" not in resp.text
    assert "closest indexed term" not in resp.text


def test_boost_unit_no_index_term(rich_index: IndexService) -> None:
    """A query with no indexed term returns the page untouched."""
    page = SearchPage(
        query="zzz not indexed", total=1, page=1, limit=50,
        hits=[SearchHit(report_id="r1", link_id="r1", date=None,
                        title="T", report_type=None, speaker=None,
                        excerpt=None)],
        provider="sprs", rendered_total=1,
    )
    out, note = apply_exact_term_boost(page=page, index=rich_index,
                                       query="zzz not indexed")
    assert out is page and note is None


# --- (2) honest results header ------------------------------------------------


def test_results_line_honest_estimate() -> None:
    """The header shows the rendered count, never a bare estimate."""
    page = SearchPage(query="q", total=2505, page=1, limit=200, hits=[],
                      provider="sprs", rendered_total=200)
    line = results_line(page=page, hits=[SearchHit(
        report_id=str(i), link_id=str(i), date=None, title="t",
        report_type=None, speaker=None, excerpt=None) for i in range(200)])
    assert line == "Showing 200 of ~2,505 estimated results"


def test_results_line_exact_when_equal() -> None:
    """When the rendered count equals the total, a plain 'Results: N'."""
    page = SearchPage(query="q", total=3, page=1, limit=50, hits=[],
                      provider="sprs", rendered_total=3)
    hits = [SearchHit(report_id=str(i), link_id=str(i), date=None, title="t",
                      report_type=None, speaker=None, excerpt=None)
            for i in range(3)]
    assert results_line(page=page, hits=hits) == "Results: 3"


def test_search_page_json_carries_rendered_total(
    client_with_index: TestClient,
) -> None:
    """format=json exposes rendered_total (exact) alongside total (estimate)."""
    rows = _hib_noise_rows()[:20]
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        route = mock.post("/searchResult").respond(json=rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            resp = client_with_index.get(
                f"/a/{TEST_TOKEN}/search?q=Health%20Information%20Bill"
                "&format=json")
        finally:
            pair.stop()
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert data["rendered_total"] == len(rows)
    assert data["total"] == 2505  # the upstream estimate is preserved


def test_html_header_on_hib_query(client_with_index: TestClient) -> None:
    """The HTML header for the HIB query is the honest 'Showing N of ~T'."""
    resp = _hib_search(
        client_with_index, query="Health%20Information%20Bill",
        rows=_hib_noise_rows(),
    )
    assert resp.status_code == 200
    # 40 noise + 3 upstream HIB (bill-773/774/775) + 1 index-pinned (b1)
    # = 44 shown, of the ~2,505 upstream estimate.
    assert "Showing 44 of ~2,505 estimated results" in resp.text
    assert "Results: 2505" not in resp.text


# --- (3) related-topics tie-break ---------------------------------------------


def test_related_terms_all_words_first(rich_index: IndexService) -> None:
    """q='Health Information': surfaces containing ALL query words sort
    before surfaces sharing only 'health'/'information' (the 'bill'
    doc_count flood no longer buries the 4-doc HIB surface)."""
    links = build_related_terms(
        token=TEST_TOKEN, query="Health Information",
        terms=rich_index.terms_by_norm(),
    )
    labels = [l["label"] for l in links]
    # The all-words tier: every 'Health Information*' surface (the 120 Budget
    # rows share no query word, so they are absent by construction).
    assert _HIB_SURFACE in labels and _HIB_PARTIAL in labels
    hib_positions = [labels.index(l) for l in labels
                     if l.startswith("Health Information")]
    # All-words tier occupies the FIRST len(tier) slots, in doc_count order.
    assert hib_positions == list(range(len(hib_positions))), (
        f"all-words tier must be a contiguous prefix: {labels[:8]}")
    # The 4-doc 'Health Information Bill' surface is visible (not flooded).
    assert _HIB_SURFACE in labels[:6]


def test_related_terms_single_word_query_partial_only() -> None:
    """A single-word query: no surface contains all words unless it IS the
    word — partial matches rank by doc_count as before."""
    # The input rows are doc_count-sorted (the index query's order).
    terms = [
        ("bill", "bill", 25000),
        ("Health Information Bill", "health information bill", 4),
        ("Income Tax Bill", "income tax bill", 3),
    ]
    links = build_related_terms(token=TEST_TOKEN, query="bill", terms=terms)
    labels = [l["label"] for l in links]
    assert labels[0] == "bill"  # the exact single-word term (all words)
    # Partial tier follows, doc_count order (25k > 4 > 3).
    assert labels[1:] == ["Health Information Bill", "Income Tax Bill"]
