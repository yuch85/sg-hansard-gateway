"""Spec §4.4 existing-route additions (Phase 27.1 wave 3).

/search gains refine-by-date / refine-by-speaker / related-topics /
pagination / R9 format siblings; /date/{day} gains prev-next-sitting +
/year/{yyyy}; /report/{id} gains a visible sitting-TOC anchor, prev/next
report, top-5 title-term search links, and format siblings. All links are
finished absolute URLs (R1/R3/R5) — the client never builds a URL itself.

Offline: TestClient + respx stubs; no real upstream call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import respx
from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import settings

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"

BASE = settings.public_base_url.rstrip("/")
FIXTURES = Path(__file__).parent / "fixtures"


def _rows() -> list[dict[str, Any]]:
    return json.loads(
        (FIXTURES / "searchresult_20041019_p1.json").read_text(encoding="utf-8")
    )


def _topic() -> dict[str, Any]:
    return json.loads(
        (FIXTURES / "topic_20041019_saf.json").read_text(encoding="utf-8")
    )


E2E_REPORT_ID = "037_20041019_S0004_T0023"


def _stub_search(mock: respx.MockRouter) -> None:
    mock.post("/searchResult").respond(json=_rows())


def _search(client: TestClient, path: str = "q=Pension%20Fund") -> Any:
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            return client.get(f"/a/{TEST_TOKEN}/search?{path}")
        finally:
            pair.stop()


# --- /search additions ------------------------------------------------------


def test_search_refine_by_date(client_with_index: TestClient) -> None:
    """Refine-by-date: decade + year links re-run the SAME q (percent-encoded)."""
    r = _search(client_with_index)
    assert r.status_code == 200
    assert "Refine by date" in r.text
    # The fixture index's years (1988..2025) must appear as finished links
    # carrying the same q (Pension Fund), percent-encoded.
    for year in ("2025", "2024", "2023", "2020", "2001", "1988"):
        assert f"from_={year}-01-01" in r.text, f"year link {year} missing"
    assert "q=Pension%20Fund" in r.text
    # Decade links (1980s, 2000s, 2020s from the fixture index).
    assert "1980s" in r.text and "2000s" in r.text and "2020s" in r.text


def test_search_refine_by_speaker_absent_for_speakerless_hits(
    client_with_index: TestClient
) -> None:
    """The 2004 fixture rows carry no mpNames — the section is omitted (not a
    stub): an empty refine-by-speaker block would be a dead-end."""
    r = _search(client_with_index)
    assert r.status_code == 200
    assert "Refine by speaker" not in r.text


def test_search_refine_by_speaker_present(
    client_with_index: TestClient
) -> None:
    """Hits WITH speakers produce top-N speaker= links (finished, encoded)."""
    rows = _rows()
    rows[0]["mpNames"] = "Dr Ong Chit Chung"
    rows[1]["mpNames"] = "Dr Ong Chit Chung"
    rows[2]["mpNames"] = "Foo Ah Kow"
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(json=rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            r = client_with_index.get(f"/a/{TEST_TOKEN}/search?q=Pension%20Fund")
        finally:
            pair.stop()
    assert r.status_code == 200
    assert "Refine by speaker" in r.text
    assert "speaker=Dr%20Ong%20Chit%20Chung" in r.text
    assert "speaker=Foo%20Ah%20Kow" in r.text
    # Frequency order (R7): the 2-hit speaker first.
    assert r.text.index("speaker=Dr%20Ong%20Chit%20Chung") < \
        r.text.index("speaker=Foo%20Ah%20Kow")


def test_search_related_topics(client_with_index: TestClient) -> None:
    """Related-topics: index terms sharing a word with q are offered.

    Uses q='health' — the fixture index has 'Health Information Bill'
    (norm 'health information bill'), so the shared word 'health' matches.
    """
    r = _search(client_with_index, path="q=health")
    assert r.status_code == 200
    assert "Related topics" in r.text
    assert f"{BASE}/a/{TEST_TOKEN}/search?q=Health%20Information%20Bill" in r.text


def test_search_pagination(client_with_index: TestClient) -> None:
    """A multi-page result set offers Previous/Next/numbered finished URLs."""
    rows = _rows()
    # Force a large total so pagination renders (the fixture's maxResult).
    # 20 rows * 7 = 140 > 20 unique, so the dedupe sweep keeps all 20 and
    # reports total=140 (3 pages at limit=50).
    for row in rows:
        row["maxResult"] = str(len(rows) * 7)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(json=rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            r = client_with_index.get(f"/a/{TEST_TOKEN}/search?q=Fund&page=1")
        finally:
            pair.stop()
    assert r.status_code == 200
    assert "Pages" in r.text
    # Next exists on page 1; it is a finished absolute URL with page=2.
    assert f"{BASE}/a/{TEST_TOKEN}/search?" in r.text
    assert "page=2" in r.text
    assert "Next" in r.text
    assert "Previous" not in r.text  # page 1 has no Previous


def test_search_pagination_last_page_no_next(client_with_index: TestClient) -> None:
    """On the last page the Next link is absent (spec §4.4).

    The SPRS provider slices the deduped rows by (page-1)*limit, so with
    20 unique rows and limit=10, page=2, total=49 → 5 pages; page 2 is
    NOT the last. To test the LAST page, use page=5: rows[40:50] is empty
    → hits=[] → no pagination rendered. Instead, verify the builder
    directly: build_pagination with page==pages yields no Next link.
    """
    from hansard_gateway.render.links import build_pagination
    links = build_pagination(
        token=TEST_TOKEN, query="test", page=5, limit=10, total=49,
    )
    labels = [l["label"] for l in links]
    assert "Previous" in labels
    assert "Next" not in labels, f"Next present on last page: {labels}"
    # And page 1 has no Previous.
    links1 = build_pagination(
        token=TEST_TOKEN, query="test", page=1, limit=10, total=49,
    )
    labels1 = [l["label"] for l in links1]
    assert "Previous" not in labels1
    assert "Next" in labels1


def test_search_format_siblings(client_with_index: TestClient) -> None:
    """R9: the current page is offered as finished ?format=json/text links."""
    r = _search(client_with_index, path="q=Pension%20Fund")
    assert r.status_code == 200
    # The query is rendered with '+' (abs_search_url uses quote(v, safe='')
    # which keeps spaces as %20; but the URL builder for self_url goes
    # through abs_search_url → %20). Check both encodings.
    assert "format=json" in r.text
    assert "format=text" in r.text
    # The format siblings are absolute + token-bearing (R1/R3).
    assert f"{BASE}/a/{TEST_TOKEN}/search?" in r.text
    # HTML-escaped ampersands (&amp;) are the correct rendering.
    assert "&amp;format=json" in r.text or "&format=json" in r.text


# --- /date additions --------------------------------------------------------


def test_date_prev_next_year_links(client_with_index: TestClient) -> None:
    """/date/{day} carries prev/next-sitting + /year/{yyyy} links (index data).

    The fixture index's sitting sequence (1988-11-15, 2001-06-20, 2004-10-19
    [from the swept rows], 2020-03-01, 2023-05-10, 2024-02-20, 2025-01-08)
    puts 2004-10-19 between 2001-06-20 and 2020-03-01.
    """
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock)
        r = client_with_index.get(f"/a/{TEST_TOKEN}/date/2004-10-19")
    assert r.status_code == 200
    assert "Previous sitting" in r.text
    assert f"{BASE}/a/{TEST_TOKEN}/date/2001-06-20" in r.text
    assert "Next sitting" in r.text
    assert f"{BASE}/a/{TEST_TOKEN}/date/2020-03-01" in r.text
    assert f"{BASE}/a/{TEST_TOKEN}/year/2004" in r.text
    # R2 twins on the new anchors.
    assert f'<span class="u">{BASE}/a/{TEST_TOKEN}/date/2001-06-20</span>' in r.text


def test_date_first_sitting_no_prev(client_with_index: TestClient) -> None:
    """The earliest index sitting has no Previous link (corpus edge)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock)
        r = client_with_index.get(f"/a/{TEST_TOKEN}/date/1988-11-15")
    assert r.status_code == 200
    assert "Previous sitting" not in r.text
    assert "Next sitting" in r.text
    assert f"{BASE}/a/{TEST_TOKEN}/date/2001-06-20" in r.text


# --- /report additions ------------------------------------------------------


def test_report_nav_links(client_with_index: TestClient) -> None:
    """/report/{id} carries the visible TOC anchor and format siblings (R9)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_topic())
        r = client_with_index.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert r.status_code == 200
    body = r.text
    # The sitting TOC link is a visible anchor (spec §4.4).
    assert "This sitting" in body
    assert f"{BASE}/a/{TEST_TOKEN}/date/2004-10-19" in body
    # Format siblings (R9).
    assert f"{BASE}/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=json" in body
    assert f"{BASE}/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=text" in body


def test_report_title_terms_with_real_title(client_with_index: TestClient) -> None:
    """A report whose title shares words with index terms gets top-5 search
    links. The 2004 fixture topic's meta Title is
    'SINGAPORE ARMED FORCES (AMENDMENT NO. 2) BILL' — the index has no
    matching terms, so the section is absent (not a stub). To exercise the
    path, patch the index with a matching term."""
    topic = _topic()
    # The parsed title (from htmlContent meta) is the SAF amendment bill.
    # The fixture index has no 'singapore armed forces' term, so no title
    # terms render. Assert the section is simply absent (correct empty
    # behaviour), and separately verify the builder works with a matching
    # term via a direct unit call.
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=topic)
        r = client_with_index.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert r.status_code == 200
    assert "Search title terms" not in r.text  # no matching index terms

    # Direct builder check: a title that DOES match index terms.
    from hansard_gateway.render.links import build_report_nav
    nav = build_report_nav(
        token=TEST_TOKEN, report_id="b1",
        title="Health Information Bill", day_iso="2023-05-10",
        reports_in_sitting=[("b1", "Health Information Bill")],
        terms=[
            ("Health Information Bill", "health information bill", 2),
            ("Health Information", "health information", 1),
        ],
    )
    assert nav["title_terms"], "title terms should be non-empty"
    assert any(
        "q=Health%20Information%20Bill" in t["url"]
        for t in nav["title_terms"]
    ), f"expected finished search link, got {nav['title_terms']}"


def test_report_prev_next_within_sitting(client_with_index: TestClient) -> None:
    """Prev/next report use the index's within-sitting ordering.

    The fixture index stores the 2004-10-19 E2E report as the ONLY report in
    that sitting, so neither prev nor next renders — the section is simply
    absent (not a broken link).
    """
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_topic())
        r = client_with_index.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert r.status_code == 200
    assert "Previous report" not in r.text
    assert "Next report" not in r.text


def test_no_new_upstream_calls_for_link_groups(
    client_with_index: TestClient
) -> None:
    """The link groups come from the local index + already-fetched hits.

    The searchResult sweep makes up to (hits + max_no_gain) calls — the
    refine/related/pagination data adds ZERO extra upstream calls (it is
    index-backed). We assert the call count is bounded by the sweep's own
    loop, not inflated by link-group computation.
    """
    rows = _rows()
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        route = mock.post("/searchResult").respond(json=rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            r = client_with_index.get(f"/a/{TEST_TOKEN}/search?q=Pension%20Fund")
        finally:
            pair.stop()
    assert r.status_code == 200
    # The sweep dedupes overlapping pages; with 20 unique rows it makes a
    # small number of calls (<= hits + max_no_gain). The link groups add
    # no calls — so the total stays bounded by the sweep alone.
    from hansard_gateway.config import settings as _s
    assert route.call_count <= len(rows) + _s.search_max_no_gain
