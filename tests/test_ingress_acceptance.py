"""Spec 8 acceptance oracle (Phase 27.1 wave 5) — the link-only client's
contract, as executable offline pytest against the TestClient.

Section 8.1 is the load-bearing backstop: a strict client simulator starts
with ONLY the launcher URL, maintains a seen-URL set extended only with URLs
literally present in a fetched body (anchors AND the visible .u span text,
per R2), and fails on any fetch outside the set. Six targets, each in
<= 6 fetches. Sections 8.2/8.3/8.4 reuse the wave-3 conformance linter and
the wave-4 dead-end fixtures; the live re-checks are in ``live``-marked
sibling tests (deselected by the default ``-m 'not live'``).

Offline: no real upstream — the search/date/report hops are stubbed (or
fixture-backed) so the simulator never touches SPRS.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import Settings, settings
from hansard_gateway.index.loader import IndexService
from hansard_gateway.main import create_app
from hansard_gateway.render.urls import abs_format_sibling, abs_report_url

from tests.conftest import (
    RICH_BACKBENCHER,
    RICH_HIT_QUERY,
    RICH_HIT_COUNT,
    RICH_SITTING_1988,
    _rich_hit_rows,
)
from tests.test_link_conformance import (
    PAIR_BASE,
    UPSTREAM_BASE,
    _INVALID_404_FIXTURE,
    _INVALID_404_MD5,
    _lint_html,
)

#: The spec 8.1 fetch budget per target.
_MAX_FETCHES = 6

#: The HIB report the spec 8.1 target 1 must reach (fixture report_id).
_HIB_REPORT_ID = "hib1"

#: The HIB transcript title that must appear in the fetched report page
#: (the committed 2004 sprs2 topic payload's title — the offline ground truth
#: for every report fetch).
_HIB_TITLE = "SINGAPORE ARMED FORCES"

#: The second-word-distinctive topic (target 2): 'Review' is the salient word.
_TOPIC2_QUERY = "Budget Review"

#: The >100-hit search query (target 5).
_TOPIC5_QUERY = RICH_HIT_QUERY

#: The 1988 sitting TOC target (target 3).
_DATE_1988_URL_SUFFIX = f"/date/{RICH_SITTING_1988}"

#: The sprs2 topic payload (offline ground truth for report fetches).
_TOPIC_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "topic_20041019_saf.json"

#: The committed searchResult rows (offline ground truth; sliced per page).
_SEARCH_ROWS_PATH = Path(__file__).parent / "fixtures" / "searchresult_20041019_p1.json"


# --- row builders (the stub returns DIFFERENT rows per page — T-27-34) -------


def _search_row(
    report_id: str, title: str, *, speaker: Optional[str] = None,
    date_iso: str = "19-10-2004", report_type: str = "oral-answer",
    max_result: Optional[int] = None,
) -> dict[str, Any]:
    """One searchResult row in the committed fixture shape (46-field rows
    normalise identically; the provider reads only these keys).

    ``max_result`` defaults to RICH_HIT_COUNT (the >100-hit corpus); the
    single-hit HIB / Budget-Review rows override it to 1 (the sweep stops
    after the first page)."""
    return {
        "reportId": report_id,
        "htmlFileName": report_id,
        "title": title,
        "sittingDate": date_iso,
        "reportVersion": "sprs2",
        "reportType": report_type,
        "mpNames": speaker,
        "maxResult": str(max_result if max_result is not None else RICH_HIT_COUNT),
    }


def _rich_hit_search_rows() -> list[dict[str, Any]]:
    """The 120 rhNNN rows as searchResult rows (the >100-hit corpus).

    Converting the index rows (conftest ``_rich_hit_rows``) to the upstream
    shape: sprs2 date format (dd-mm-yyyy), maxResult=RICH_HIT_COUNT on every
    row (the sweep's ``total = max(maxResult)`` needs it on each page)."""
    rows: list[dict[str, Any]] = []
    for i in range(RICH_HIT_COUNT):
        src = _rich_hit_rows()[i]
        y, m, d = src["sitting_date"].split("-")
        rows.append(
            {
                "reportId": src["report_id"],
                "htmlFileName": src["report_id"],
                "title": src["title"],
                "sittingDate": f"{d}-{m}-{y}",
                "reportVersion": "sprs2",
                "reportType": src["report_type"],
                "mpNames": src["speaker"],
                "maxResult": str(RICH_HIT_COUNT),
            }
        )
    return rows


def _rich_search_rows(keyword: str) -> list[dict[str, Any]]:
    """The stub row set for a search: the HIB hit, the target-2 topic, and —
    for the >100-hit query — 120 distinct Budget rows (page 3 = rows 100-119).

    The rows are sliced by (startIndex, endIndex) in :func:`_stub_rich_search`
    (T-27-34 — a static stub cannot exercise pagination slicing). The
    single-hit rows carry maxResult=1 so their sweep terminates on page 1;
    the 120-row corpus carries maxResult=RICH_HIT_COUNT so page 3 = rows
    100-119."""
    rows = [
        _search_row("hib1", "Health Information Bill", report_type="bill",
                    max_result=1),
        _search_row("rd1", "Budget Review", max_result=1),
        _search_row("bud0", "Budget", max_result=1),
    ]
    if keyword == _TOPIC5_QUERY:
        rows.extend(_rich_hit_search_rows())
    return rows


# --- the strict seen-URL simulator (spec 8.1) --------------------------------


class _UrlParser(HTMLParser):
    """Collects every URL literally present in a body: anchor hrefs AND the
    visible .u span text (R2 belt-and-braces — a target reachable ONLY via a
    .u span still counts)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self._in_u = False
        self._u_text = ""

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        d = dict(attrs)
        if tag == "a" and d.get("href", "").startswith("http"):
            self.urls.append(d["href"])
        elif tag == "span" and d.get("class") == "u":
            self._in_u = True
            self._u_text = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._in_u:
            self.urls.append(self._u_text.strip())
            self._in_u = False

    def handle_data(self, data: str) -> None:
        if self._in_u:
            self._u_text += data


class StrictClient:
    """The spec 8.1 link-only client: seen starts as {launcher} and grows
    only with URLs literally present in a fetched body. Any fetch outside the
    set is a test failure (the no-URL-construction invariant)."""

    def __init__(self, client: TestClient, *, token: str, base: str) -> None:
        self._client = client
        self._base = base.rstrip("/")
        self._token = token
        self.seen: set[str] = {base.rstrip("/") + f"/a/{token}/"}
        self.fetches = 0

    def fetch(self, url: str) -> httpx.Response:
        """Fetch ``url`` — a failure if it was never literally rendered."""
        if url not in self.seen:
            raise AssertionError(
                f"fetch of URL not literally present in any fetched body "
                f"(no-URL-construction violation): {url!r}"
            )
        path = "/" + url.split(self._base, 1)[1].lstrip("/")
        response = self._client.get(path)
        self.fetches += 1
        parser = _UrlParser()
        parser.feed(response.text)
        for candidate in parser.urls:
            if candidate.startswith(self._base):
                self.seen.add(candidate)
        return response

    def pick(self, response: httpx.Response, predicate) -> httpx.Response:
        """Re-fetch the first URL in ``response``'s body matching ``predicate``."""
        parser = _UrlParser()
        parser.feed(response.text)
        for candidate in parser.urls:
            if predicate(candidate):
                return self.fetch(candidate)
        raise AssertionError(
            f"no URL in body matches predicate; body had {len(parser.urls)} URLs"
        )

    def reset(self) -> None:
        """A fresh walk from the launcher (each target counts its own fetches)."""
        self.seen = {self._base + f"/a/{self._token}/"}
        self.fetches = 0


def _launch(sim: StrictClient) -> httpx.Response:
    """Fetch the launcher (fetch #1 of every walk)."""
    return sim.fetch(next(iter(sim.seen)))


# --- fixtures ----------------------------------------------------------------


def _topic_fixture() -> dict[str, Any]:
    """The committed 2004 sprs2 topic payload (the report transcript)."""
    import json

    return json.loads(_TOPIC_FIXTURE_PATH.read_text(encoding="utf-8"))


def _rich_client(token_store, rich_index: IndexService) -> TestClient:
    """The app over the spec 8.1 corpus with the fixture token store."""
    import hansard_gateway.auth as auth_mod

    auth_mod._store = token_store
    return TestClient(create_app(index_override=rich_index))


def _stub_rich_search(mock: respx.MockRouter) -> None:
    """Stub the upstream searchResult: keyword + start_index keyed rows.

    The stub returns DIFFERENT rows per page (T-27-34 — static stubs cannot
    exercise pagination slicing): the 120-row corpus is sliced by
    (start_index, end_index) so page N of the sweep yields rows N.
    """
    import json

    rows_by_keyword: dict[str, list[dict[str, Any]]] = {}

    def _rows(keyword: str) -> list[dict[str, Any]]:
        if keyword not in rows_by_keyword:
            rows_by_keyword[keyword] = _rich_search_rows(keyword)
        return rows_by_keyword[keyword]

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        keyword = str(body.get("keyword") or "")
        mp_name = str(body.get("mpName") or "")
        start = int(body.get("startIndex") or 0)
        end = int(body.get("endIndex") or start)
        rows = _rows(keyword)
        # Filter by speaker when the search is speaker-scoped (target 4).
        # The rh000 row (in the Budget corpus) carries the backbencher; the
        # single-hit rows do not.
        if mp_name:
            rows = [r for r in rows if r.get("mpNames") == mp_name]
            if not rows:
                # The rh corpus is only in _rows("Budget") — fall back to it.
                rows = [r for r in _rows(_TOPIC5_QUERY)
                        if r.get("mpNames") == mp_name]
        page = [r for r in rows if start <= _row_index(r) <= end]
        return httpx.Response(200, json=page)

    mock.post("/searchResult").mock(side_effect=_handler)


def _row_index(row: dict[str, Any]) -> int:
    """The stable per-row position within the 120-row corpus (rhNNN rows).

    The single-hit HIB / rd rows index at 0 (they are the only rows for
    their keyword — the slice returns them for the first page window)."""
    rid = str(row.get("reportId") or "")
    if rid.startswith("rh"):
        return int(rid[2:])
    return 0


def _stub_topics(mock: respx.MockRouter, *, hib: bool = True) -> None:
    """Stub getHansardTopic for the corpus report ids (offline transcript)."""
    payload = _topic_fixture()
    mock.post("/getHansardTopic").respond(json=payload)


def _pair_stub() -> respx.MockRouter:
    """The Pair discovery stub (no hits — SPRS is authoritative offline)."""
    return respx.mock(
        base_url=PAIR_BASE, assert_all_called=False, assert_all_mocked=False
    )


@pytest.fixture()
def rich_sim(token_store, rich_index: IndexService) -> StrictClient:
    """The strict simulator over the spec 8.1 corpus + stubbed upstream."""
    client = _rich_client(token_store, rich_index)
    base = settings.public_base_url.rstrip("/")
    sim = StrictClient(client, token=TEST_TOKEN, base=base)
    mock = respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False)
    _stub_rich_search(mock)
    _stub_topics(mock)
    pair = _pair_stub()
    mock.start()
    pair.start()
    try:
        yield sim
    finally:
        pair.stop()
        mock.stop()


# --- section 8.1 reachability (six targets, each <= 6 fetches) ---------------
#
# The ladder's Block B children are the 1-char extensions of the current
# prefix (nav/r -> re/ri/ro/ru... — per-word, spec §3.4/§4.2), and Block A
# lists every term reachable at the current prefix — so a word of length L
# takes L-1 fetches off the letter index (letter -> ... -> L-1 chars), after
# which the term itself is a rendered search link. The simulator NEVER builds
# a prefix: it follows only what is literally rendered.


def _nav_pick(sim: StrictClient, response: httpx.Response, child: str) -> httpx.Response:
    """Follow the rendered 2-char child link ``.../nav/{child}``."""
    return sim.pick(response, lambda u: u.rstrip("/").endswith(f"/nav/{child}"))


def _q_pick(sim: StrictClient, response: httpx.Response, query: str) -> httpx.Response:
    """Follow the rendered finished search link for ``query``."""
    return sim.pick(response, lambda u: f"q={query}" in u)


def test_81_target1_hib_report_transcript(rich_sim: StrictClient) -> None:
    """Launcher -> /nav/he -> /nav/hea (Block A: Health Information) ->
    search -> HIB report (transcript present), in <= 6 fetches."""
    sim = rich_sim
    launcher = _launch(sim)
    nav_h = sim.pick(launcher, lambda u: u.rstrip("/").endswith("/nav/h"))
    nav_he = sim.pick(nav_h, lambda u: u.rstrip("/").endswith("/nav/he"))
    hib_search = _q_pick(sim, nav_he, "Health%20Information")
    report = sim.pick(
        hib_search, lambda u: u.rstrip("/").endswith(f"/report/{_HIB_REPORT_ID}")
    )
    assert report.status_code == 200
    assert _HIB_TITLE in report.text
    assert "Transcript SHA-256" in report.text  # the transcript footer
    assert sim.fetches <= _MAX_FETCHES, f"{sim.fetches} fetches > {_MAX_FETCHES}"


def test_81_target2_second_word_topic(rich_sim: StrictClient) -> None:
    """Walk the ladder on the SECOND word ('review'), per spec 8.1 target 2:
    launcher -> /nav/r -> /nav/re -> /nav/rev -> /nav/revi -> /nav/revie —
    the page for the first 5 chars of 'review' lists 'Budget Review' in
    Block A (<= 6 fetches, strict seen-URL rules).

    The child fan-out is per-word (spec §3.4/§4.2): 're' is a child of 'r'
    because 'review' is a word of the term's norm, not because 're' is the
    term's second whole-norm character. (The wave-1 whole-norm fan-out
    missed this — the rectify-271-ladder-word-fanout fix.)"""
    sim = rich_sim
    launcher = _launch(sim)
    nav_r = sim.pick(launcher, lambda u: u.rstrip("/").endswith("/nav/r"))
    nav_re = sim.pick(nav_r, lambda u: u.rstrip("/").endswith("/nav/re"))
    nav_rev = sim.pick(nav_re, lambda u: u.rstrip("/").endswith("/nav/rev"))
    nav_revi = sim.pick(nav_rev, lambda u: u.rstrip("/").endswith("/nav/revi"))
    nav_revie = sim.pick(nav_revi, lambda u: u.rstrip("/").endswith("/nav/revie"))
    assert sim.fetches <= _MAX_FETCHES, (
        f"{sim.fetches} fetches > {_MAX_FETCHES}"
    )
    assert _TOPIC2_QUERY in nav_revie.text, (
        "the ladder page for the second word's first 5 chars must list "
        "'Budget Review' (the second-word-distinctive topic)"
    )


def test_81_target3_1988_sitting_toc(rich_sim: StrictClient) -> None:
    """Launcher -> /years -> /year/1988 -> /date/{1988 sitting} TOC."""
    sim = rich_sim
    launcher = _launch(sim)
    years = sim.pick(launcher, lambda u: u.rstrip("/").endswith("/years"))
    year_1988 = sim.pick(years, lambda u: u.rstrip("/").endswith("/year/1988"))
    toc = sim.pick(
        year_1988, lambda u: _DATE_1988_URL_SUFFIX in u
    )
    assert toc.status_code == 200
    assert RICH_SITTING_1988 in toc.text
    assert sim.fetches <= _MAX_FETCHES, f"{sim.fetches} fetches > {_MAX_FETCHES}"


def test_81_target4_backbencher_reports(rich_sim: StrictClient) -> None:
    """Launcher -> /members -> /members/l -> the finished speaker search ->
    the backbencher's report."""
    sim = rich_sim
    launcher = _launch(sim)
    members = sim.pick(launcher, lambda u: u.rstrip("/").endswith("/members"))
    letter_l = sim.pick(members, lambda u: u.rstrip("/").endswith("/members/l"))
    speaker_search = sim.pick(
        letter_l, lambda u: f"speaker={RICH_BACKBENCHER.replace(' ', '%20')}" in u
    )
    report = sim.pick(
        speaker_search,
        lambda u: u.rstrip("/").endswith("/report/rh000"),
    )
    assert report.status_code == 200
    assert RICH_BACKBENCHER in speaker_search.text
    assert sim.fetches <= _MAX_FETCHES, f"{sim.fetches} fetches > {_MAX_FETCHES}"


def test_81_target5_page3_of_100plus_search(rich_sim: StrictClient) -> None:
    """Launcher -> /nav/bu -> /nav/bud (Block A) -> Budget search (120 hits)
    -> page 3 (rows 100-119), via rendered pagination links only."""
    sim = rich_sim
    launcher = _launch(sim)
    nav_b = sim.pick(launcher, lambda u: u.rstrip("/").endswith("/nav/b"))
    nav_bu = sim.pick(nav_b, lambda u: u.rstrip("/").endswith("/nav/bu"))
    search1 = _q_pick(sim, nav_bu, _TOPIC5_QUERY)
    page3 = sim.pick(
        search1,
        lambda u: "page=3" in u and _TOPIC5_QUERY in unquote(u),
    )
    assert page3.status_code == 200
    # 27.1-search-hop-rectify: the COLD Budget sweep (120 probed > the 5-page
    # budget of 100 rows) is TRUNCATED — it collects ~100 rows (rh000-rh099 +
    # the 3 non-rh Budget rows) and stops. Page 3 is the tail of the
    # COLLECTED rows: rh100-rh102 don't exist (only 100 rh rows collected),
    # so page 3 = the pinned exact-term bud0 + the last collected rh rows
    # (rh098-rh099) = 4 shown, of the ~120 upstream estimate. The F-4 honest
    # header shows the rendered count, never the bare estimate; the truncated
    # note appears on page 1 (the continuation pages are within the collected
    # rows). Pagination stayed click-only (page 3 reached via a rendered link).
    assert "Showing 4 of ~120 estimated results" in page3.text
    assert f"Results: {RICH_HIT_COUNT}" not in page3.text
    # Page 1 carries the honest truncated-sweep note.
    assert "stopped after the first page budget" in search1.text
    assert sim.fetches <= _MAX_FETCHES, f"{sim.fetches} fetches > {_MAX_FETCHES}"


def test_81_target6_json_rendering_of_report(
    rich_sim: StrictClient,
) -> None:
    """From the test-1 report page, follow its rendered ?format=json sibling
    link to the JSON rendering."""
    sim = rich_sim
    launcher = _launch(sim)
    nav_h = sim.pick(launcher, lambda u: u.rstrip("/").endswith("/nav/h"))
    nav_he = sim.pick(nav_h, lambda u: u.rstrip("/").endswith("/nav/he"))
    hib_search = _q_pick(sim, nav_he, "Health%20Information")
    report = sim.pick(
        hib_search, lambda u: u.rstrip("/").endswith(f"/report/{_HIB_REPORT_ID}")
    )
    json_url = abs_format_sibling(
        url=abs_report_url(token=TEST_TOKEN, link_id=_HIB_REPORT_ID), fmt="json"
    )
    body = sim.fetch(json_url)
    assert body.status_code == 200
    assert body.headers["content-type"].startswith("application/json")
    assert sim.fetches <= _MAX_FETCHES, f"{sim.fetches} fetches > {_MAX_FETCHES}"


# --- section 8.2 dead-end sweep (offline) ------------------------------------
#
# Reuses the wave-4 dead-end fixtures (respx 502 stub, low-budget 429, the
# no-results topic marker) and the wave-3 linter. Each failure class must
# carry >=1 absolute token-bearing URL that returns 200 when followed; the
# invalid-token 404 must stay byte-identical to the production fixture.


#: A well-formed report id the stubbed upstream does not hold (valid-token 404).
_UNKNOWN_REPORT_ID = "bill-999"

#: The no-results marker (wave-4: the live unknown-id path).
_NO_RESULTS_MARKER = "No Results Found"


def _follow_first_token_link(
    client: TestClient, body: str, *, base: str, token: str
) -> httpx.Response:
    """Follow the first absolute token-bearing URL in ``body``; assert 200."""
    parser = _UrlParser()
    parser.feed(body)
    candidates = [
        u for u in parser.urls
        if u.startswith(base) and f"/a/{token}/" in u
    ]
    assert candidates, f"no absolute token-bearing URL in body: {body[:200]}"
    url = candidates[0]
    path = "/" + url.split(base, 1)[1].lstrip("/")
    response = client.get(path)
    assert response.status_code == 200, (
        f"token-bearing link {url!r} returned {response.status_code}"
    )
    return response


def test_82_zero_result_search_recoverable(
    token_store, rich_index: IndexService
) -> None:
    """A zero-hit /search carries >=1 absolute token-bearing link -> 200."""
    client = _rich_client(token_store, rich_index)
    base = settings.public_base_url.rstrip("/")
    mock = respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False)
    mock.post("/searchResult").respond(json=[])
    pair = _pair_stub()
    mock.start()
    pair.start()
    try:
        r = client.get(f"/a/{TEST_TOKEN}/search?q=zzzqqq")
    finally:
        pair.stop()
        mock.stop()
    assert r.status_code == 200
    assert "No results found" in r.text
    _follow_first_token_link(client, r.text, base=base, token=TEST_TOKEN)


def test_82_valid_token_404_recoverable(
    token_store, rich_index: IndexService
) -> None:
    """A valid-token 404 (unknown well-formed id) carries >=1 absolute
    token-bearing link -> 200."""
    import json as _json

    client = _rich_client(token_store, rich_index)
    base = settings.public_base_url.rstrip("/")
    mock = respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False)
    mock.post("/getHansardTopic").respond(
        status_code=500, content=_json.dumps({"resultHTML": _NO_RESULTS_MARKER})
    )
    pair = _pair_stub()
    mock.start()
    pair.start()
    try:
        r = client.get(f"/a/{TEST_TOKEN}/report/{_UNKNOWN_REPORT_ID}")
    finally:
        pair.stop()
        mock.stop()
    assert r.status_code == 404
    _follow_first_token_link(client, r.text, base=base, token=TEST_TOKEN)


def test_82_422_malformed_prefix_recoverable(
    token_store, rich_index: IndexService
) -> None:
    """A 422 (malformed nav prefix) carries >=1 absolute token-bearing link
    -> 200."""
    client = _rich_client(token_store, rich_index)
    base = settings.public_base_url.rstrip("/")
    r = client.get(f"/a/{TEST_TOKEN}/nav/abcdef")
    assert r.status_code == 422
    _follow_first_token_link(client, r.text, base=base, token=TEST_TOKEN)


def test_82_429_recoverable(token_store, rich_index: IndexService) -> None:
    """A 429 (low per-minute budget) carries >=1 absolute token-bearing link
    -> 200. The rate budget is per-process, so this gets a fresh app."""
    import hansard_gateway.auth as auth_mod

    low_budget = Settings(per_token_rate_per_min=2)
    auth_mod._store = token_store
    client = TestClient(create_app(app_settings=low_budget,
                                   index_override=rich_index))
    base = settings.public_base_url.rstrip("/")
    mock = respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False)
    mock.post("/searchResult").respond(json=[])
    pair = _pair_stub()
    mock.start()
    pair.start()
    try:
        resp = None
        for _ in range(4):
            resp = client.get(f"/a/{TEST_TOKEN}/search?q=probe")
            if resp.status_code == 429:
                break
    finally:
        pair.stop()
        mock.stop()
    assert resp is not None and resp.status_code == 429
    _follow_first_token_link(client, resp.text, base=base, token=TEST_TOKEN)


def test_82_502_recoverable(token_store, rich_index: IndexService) -> None:
    """A 502 (upstream failure) carries >=1 absolute token-bearing link
    -> 200."""
    client = _rich_client(token_store, rich_index)
    base = settings.public_base_url.rstrip("/")
    mock = respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False)
    mock.post("/getHansardTopic").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    pair = _pair_stub()
    mock.start()
    pair.start()
    try:
        r = client.get(f"/a/{TEST_TOKEN}/report/{_UNKNOWN_REPORT_ID}")
    finally:
        pair.stop()
        mock.stop()
    assert r.status_code in (502, 503)
    _follow_first_token_link(client, r.text, base=base, token=TEST_TOKEN)


def test_82_invalid_token_404_byte_identical(
    token_store, rich_index: IndexService
) -> None:
    """The invalid-token 404 body is byte-identical to the production fixture
    (md5 9e8affead8146656e4f9c68336d598de)."""
    import hashlib

    client = _rich_client(token_store, rich_index)
    fixture = _INVALID_404_FIXTURE.read_bytes()
    assert hashlib.md5(fixture).hexdigest() == _INVALID_404_MD5
    r = client.get(f"/a/hg_bogustoken00000000000000zz/report/{_UNKNOWN_REPORT_ID}")
    assert r.status_code == 404
    assert r.content == fixture, "invalid-token 404 drifted from production body"


# --- section 8.3 link linter (offline crawler) -------------------------------
#
# BFS over rendered links from the launcher; every HTML anchor passes the
# wave-3 R1/R2/R3/R5/R6 linter + the target returns 200; every HTML page
# passes R4 + budgets + nav bar top+bottom. Non-HTML (json/text) and error
# (4xx/5xx) pages are skipped (they are not rendered HTML anchors). The
# upstream is stubbed so the crawler is offline.


def _bfs_lint(
    client: TestClient, *, base: str, token: str, max_pages: int = 200
) -> int:
    """BFS from the launcher; lint every HTML page; return the page count."""
    from collections import deque

    seen: set[str] = set()
    queue: deque[str] = deque([base.rstrip("/") + f"/a/{token}/"])
    pages = 0
    while queue and pages < max_pages:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        path = "/" + url.split(base, 1)[1].lstrip("/")
        r = client.get(path)
        if r.status_code != 200:
            continue  # error pages are linted by the 8.2 sweep, not here
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype:
            continue  # json/text siblings are not HTML anchor pages
        pages += 1
        problems = _lint_html(r.text, token=token, base=base)
        assert not problems, (
            f"{path} conformance failures:\n" + "\n".join(problems)
        )
        parser = _UrlParser()
        parser.feed(r.text)
        for candidate in parser.urls:
            if candidate.startswith(base) and candidate not in seen:
                queue.append(candidate)
    return pages


def test_83_link_linter_bfs(token_store, rich_index: IndexService) -> None:
    """The BFS crawler walks every route from the launcher; every HTML anchor
    passes R1/R2/R3/R5/R6 + target-200; every HTML page passes R4 + budgets
    + nav bar top+bottom (wave-3 linter reused)."""
    client = _rich_client(token_store, rich_index)
    base = settings.public_base_url.rstrip("/")
    mock = respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False)
    _stub_rich_search(mock)
    _stub_topics(mock)
    pair = _pair_stub()
    mock.start()
    pair.start()
    try:
        pages = _bfs_lint(client, base=base, token=TEST_TOKEN)
    finally:
        pair.stop()
        mock.stop()
    assert pages >= 10, f"BFS only reached {pages} pages (expected >=10)"


# --- section 8.4 ladder coverage ---------------------------------------------
#
# 500 randomly-sampled terms (fixed seed) each reachable via the /nav/ page
# of the first 5 chars of every non-stopword word they contain; any depth-5
# prefix whose term list exceeds 300 is reported.


def test_84_ladder_coverage(token_store, rich_index: IndexService) -> None:
    """500 random terms (seed 27) each reachable via the /nav/ page of the
    first 5 chars of every non-stopword word; over-cap depth-5 prefixes are
    reported."""
    import random
    import sqlite3

    from hansard_gateway.index.extract import STOPWORDS

    client = _rich_client(token_store, rich_index)
    base = settings.public_base_url.rstrip("/")
    mock = respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False)
    _stub_rich_search(mock)
    _stub_topics(mock)
    pair = _pair_stub()
    mock.start()
    pair.start()
    try:
        conn = sqlite3.connect(
            f"file:{rich_index._path}?mode=ro", uri=True
        )
        terms = [
            (surface, norm)
            for surface, norm in conn.execute(
                "SELECT surface, norm FROM term"
            ).fetchall()
        ]
        conn.close()
        sample = random.Random(27).sample(terms, k=min(500, len(terms)))
        over_cap: list[tuple[str, int]] = []
        for surface, norm in sample:
            for word in norm.split(" "):
                if not word or word in STOPWORDS:
                    continue
                prefix = word[:5]
                r = client.get(f"/a/{TEST_TOKEN}/nav/{prefix}")
                assert r.status_code == 200, f"/nav/{prefix} returned 404"
                assert surface in r.text, (
                    f"term {surface!r} not on /nav/{prefix} "
                    f"(word {word!r})"
                )
                # Report any depth-5 prefix whose term list exceeds 300.
                if len(prefix) == 5:
                    n = len(rich_index.terms_for_prefix(prefix))
                    if n > settings.ladder_term_cap:
                        over_cap.append((prefix, n))
        if over_cap:
            # Reported, not silently skipped (spec 8.4).
            for prefix, n in over_cap:
                print(f"OVER-CAP depth-5 prefix {prefix!r}: {n} terms")
    finally:
        pair.stop()
        mock.stop()
