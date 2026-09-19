"""Spec §6 dead-end elimination (Phase 27.1 wave 4).

Every failure class keeps navigation alive: zero-result recovery
(did-you-mean / broader / ladder entry / nav bar), the 404 split
(invalid-token byte-identical vs valid-token-unknown-id with the nav bar),
422 corrected forms, 429/502/503 retry guidance, upstream-failure resilience
for the index-only ladder/facet routes, and pagination/limit finished links.

Offline: TestClient + respx stubs; no real upstream call.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import Settings, settings
from hansard_gateway.index.build import build_index
from hansard_gateway.index.loader import IndexService
from hansard_gateway.main import create_app
from hansard_gateway.render import did_you_mean
from hansard_gateway.render.recovery_links import (
    build_limit_links,
    build_zero_result_recovery,
    corrected_form_for_422,
)

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"

#: The production invalid-token 404 fingerprint (anti-enumeration, spec §6.2).
_BOGUS_TOKEN = "hg_invalidtoken00000000000000zz"
_INVALID_404_MD5 = "1a29cc1330d50031993c3cbcde2318d7"
_INVALID_404_FIXTURE = Path(__file__).parent / "fixtures" / "invalid_token_404_body.html"

#: A zero-hit searchResult page (offline stub body).
_EMPTY_ROWS: list[dict] = []


# --- fixtures ----------------------------------------------------------------


@pytest.fixture()
def rich_index(tmp_path: Path) -> IndexService:
    """A fixture index whose terms exercise all three ranking tiers."""
    rows = [
        {"report_id": "r1", "link_id": "r1", "sitting_date": "2023-05-10",
         "title": "Data Protection and Cybersecurity", "report_type": "bill",
         "speaker": None},
        {"report_id": "r2", "link_id": "r2", "sitting_date": "2023-05-10",
         "title": "Data Protection and Cybersecurity (Amendment No. 2)",
         "report_type": "bill", "speaker": None},
        {"report_id": "r3", "link_id": "r3", "sitting_date": "2024-01-02",
         "title": "Cybersecurty", "report_type": "oral-answer",
         "speaker": None},
        {"report_id": "r4", "link_id": "r4", "sitting_date": "2024-02-20",
         "title": "Data Privacy Act", "report_type": "written-answer",
         "speaker": None},
        {"report_id": "r5", "link_id": "r5", "sitting_date": "2024-03-01",
         "title": "Economic Recovery and Jobs", "report_type": "oral-answer",
         "speaker": None},
    ]
    target = tmp_path / "rich_index.db"
    build_index(rows, target)
    return IndexService(path=target)


@pytest.fixture()
def client(token_store, rich_index: IndexService) -> TestClient:
    """The app with the rich index + the fixture token store."""
    import hansard_gateway.auth as auth_mod

    auth_mod._store = token_store
    return TestClient(create_app(index_override=rich_index))


def _stub_empty_search(mock: respx.MockRouter) -> None:
    mock.post("/searchResult").respond(json=_EMPTY_ROWS)


# --- did_you_mean (stdlib DL) --------------------------------------------------


def test_dl_distance_basics() -> None:
    """OSA distance: transposition = 1, band cap = max_dist + 1."""
    dl = did_you_mean.osa_distance
    assert dl("ab", "ab") == 0
    assert dl("ab", "ba") == 1
    assert dl("ca", "ac") == 1
    assert dl("kitten", "sitting") == 3
    assert dl("abcd", "efgh") == 3
    assert dl("cybersecurity", "cybersecurty") == 1


def test_suggest_tier_ordering(rich_index: IndexService) -> None:
    """Exact word overlap > DL distance <= 2 > shared 3-grams (spec §6.1)."""
    rows = did_you_mean.suggest_terms(
        query="cybersecurity", index=rich_index, limit=settings.did_you_mean_n
    )
    surfaces = [s for s, _n in rows]
    # Tier 0 (exact word overlap) comes first — both stored terms share
    # 'cybersecurity'; ties break on surface.
    assert surfaces[0] == "Data Protection and Cybersecurity"
    assert surfaces[1] == "Data Protection and Cybersecurity (Amendment No. 2)"
    # Tier 1 (DL distance 1 — the misspelled 'Cybersecurty') after tier 0.
    assert surfaces[2] == "Cybersecurty"
    # No tier-2 (shared-3-gram-only) candidates exist in this index — the
    # 'Data Privacy Act' term has no 3-gram overlap with 'cybersecurity' and
    # is excluded by the length band. The ordering tier-0 < tier-1 is the
    # spec §6.1 contract; tier-2 is exercised by the broader test below.
    assert len(surfaces) == 3


def test_suggest_tier2_shared_3gram() -> None:
    """Tier 2 (shared 3-grams, no DL <= 2) ranks after tier 0/1."""
    from hansard_gateway.render.did_you_mean import suggest_terms

    class _Idx:
        def terms_by_norm(self):
            return [
                ("Exact Match Term", "exact match term", 5),
                ("Close Term", "clozr term", 3),
                ("Gram Term", "grmm term", 1),
            ]

    rows = suggest_terms(query="exact close gram", index=_Idx(), limit=20)
    surfaces = [s for s, _n in rows]
    # 'exact' is an exact word overlap -> tier 0.
    assert surfaces[0] == "Exact Match Term"
    # 'close' vs 'clozr' is DL distance 1 -> tier 1.
    assert surfaces[1] == "Close Term"
    # 'gram' vs 'grmm' shares 3-grams but DL > 2 -> tier 2.
    assert surfaces[2] == "Gram Term"


def test_suggest_reads_only_term_table(rich_index: IndexService) -> None:
    """The module imports nothing beyond stdlib + the term-table query
    (prohibition: no summarisation, no new dependency)."""
    import hansard_gateway.render.did_you_mean as m

    src = Path(m.__file__).read_text(encoding="utf-8")
    for banned in ("import difflib", "python-Levenshtein", "damerau_levenshtein",
                   "import numpy", "import requests", "import httpx"):
        assert banned not in src, f"unexpected dependency surface: {banned}"
    # suggest_terms touches ONLY terms_by_norm on the index (source assertion).
    fn_src = src[src.index("def suggest_terms"):]
    for attr in ("terms_for_prefix", "common_terms", "letter_counts"):
        assert attr not in fn_src, f"suggest_terms must not query {attr}"


# --- zero-result recovery (spec §6.1) ------------------------------------------


def test_zero_result_recovery_page(client: TestClient) -> None:
    """A zero-hit /search renders did-you-mean + broader + ladder entry +
    the nav bar — NOT the bare 'No results found.' dead end."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_empty_search(mock)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            # Multi-word: 'cybersecurity' earns did-you-mean + ladder;
            # 'data' (a word of stored terms) earns a broader link.
            r = client.get(f"/a/{TEST_TOKEN}/search?q=cybersecurity%20data")
        finally:
            pair.stop()
    assert r.status_code == 200
    body = r.text
    assert "No results found" in body
    assert "Did you mean" in body
    assert "Broader searches" in body
    assert "Find it in the A-Z index" in body
    # Did-you-mean: finished search links for stored terms.
    assert f"/a/{TEST_TOKEN}/search?q=Data%20Protection%20and%20Cybersecurity" in body
    assert f"/a/{TEST_TOKEN}/search?q=Cybersecurty" in body
    # Broader: per-word link for the word that exists in the index.
    assert f"/a/{TEST_TOKEN}/search?q=data" in body
    # Ladder entry: /nav/{first 3 chars} of each query word.
    assert f"/a/{TEST_TOKEN}/nav/cyb" in body
    assert f"/a/{TEST_TOKEN}/nav/dat" in body
    # The global nav bar (top + bottom).
    assert body.count("Navigate:") == 2
    # The page size links ride along (spec §6.5).
    assert "limit=100" in body and "limit=200" in body


def test_zero_result_broader_multi_word(client: TestClient) -> None:
    """A multi-word zero-hit query offers per-word links for words that
    exist in the term index."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_empty_search(mock)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            r = client.get(f"/a/{TEST_TOKEN}/search?q=data%20economy")
        finally:
            pair.stop()
    assert r.status_code == 200
    # 'data' exists in the index; 'economy' does not.
    assert f"/a/{TEST_TOKEN}/search?q=data" in r.text
    assert f"/a/{TEST_TOKEN}/search?q=economy" not in r.text
    # Ladder entry for BOTH words (first 3 chars of each).
    assert f"/a/{TEST_TOKEN}/nav/dat" in r.text
    assert f"/a/{TEST_TOKEN}/nav/ec" in r.text


def test_zero_result_builder_broader_only_multi_word(rich_index: IndexService) -> None:
    """Broader is empty for a single-word query (spec §6.1: multi-word only).
    'data' IS a word of the stored multi-word terms, so it earns a broader
    link; 'economy' is not."""
    recovery = build_zero_result_recovery(
        token=TEST_TOKEN, query="cybersecurity", index=rich_index
    )
    assert recovery["broader"] == []
    assert len(recovery["ladder"]) == 1
    recovery2 = build_zero_result_recovery(
        token=TEST_TOKEN, query="data economy", index=rich_index
    )
    assert [l["label"] for l in recovery2["broader"]] == ["data"]
    # Both words earn ladder-entry links.
    assert [l["label"] for l in recovery2["ladder"]] == ["data", "economy"]


# --- 404 split (spec §6.2) ------------------------------------------------------


def test_invalid_token_404_byte_identical(client: TestClient) -> None:
    """The tokenless 404 stays byte-identical to the captured production body
    (md5 + fixture), nav-free, token-free."""
    fixture = _INVALID_404_FIXTURE.read_bytes()
    assert hashlib.md5(fixture).hexdigest() == _INVALID_404_MD5
    r = client.get(f"/a/{_BOGUS_TOKEN}/report/bill-999")
    assert r.status_code == 404
    assert r.content == fixture
    assert "Navigate:" not in r.text
    assert f"/a/{TEST_TOKEN}/" not in r.text


def test_valid_token_unknown_id_404_distinct(client: TestClient) -> None:
    """A well-formed id SPRS does not hold → 404 WITH the nav bar + a
    token-bearing recovery link — DISTINCT from the invalid-token body.

    SPRS's unknown-id 500 body is stubbed with the no-results marker (the
    client maps it to UpstreamError(status=404) — the live unknown-id path)."""
    import json as _json

    from hansard_gateway.sprs.client import _NO_RESULTS_MARKER

    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(
            status_code=500,
            content=_json.dumps({"resultHTML": _NO_RESULTS_MARKER}))
        r = client.get(f"/a/{TEST_TOKEN}/report/bill-999")
    assert r.status_code == 404
    fixture = _INVALID_404_FIXTURE.read_bytes()
    assert r.content != fixture, "valid-token 404 must differ from the tokenless one"
    assert "Navigate:" in r.text  # nav bar (top + bottom)
    assert f"/a/{TEST_TOKEN}/" in r.text  # token-bearing links
    assert "bill-999" in r.text  # the unknown id is named (in the detail line)
    # The invalid-token 404 for the SAME id stays byte-identical.
    r2 = client.get(f"/a/{_BOGUS_TOKEN}/report/bill-999")
    assert r2.content == fixture


def test_malformed_report_id_stays_422(client: TestClient) -> None:
    """Malformed ids (charset violation) remain 422 — only unknown
    well-formed ids move to the nav-bar 404."""
    r = client.get(f"/a/{TEST_TOKEN}/report/bad id")
    assert r.status_code == 422
    assert "Navigate:" in r.text
    assert "not a valid report id" in r.text


# --- 422 corrected forms (spec §6.3) --------------------------------------------


def test_422_nav_prefix_truncation(client: TestClient) -> None:
    """A 6-char prefix 422 links to its 5-char truncation + names the error."""
    r = client.get(f"/a/{TEST_TOKEN}/nav/abcdef")
    assert r.status_code == 422
    assert "Navigate:" in r.text
    assert f"/a/{TEST_TOKEN}/nav/abcde" in r.text
    assert "abcdef" in r.text


def test_422_nav_invalid_shape_no_correction(client: TestClient) -> None:
    """A non-inferable prefix (uppercase) 422 carries the nav bar + line only."""
    r = client.get(f"/a/{TEST_TOKEN}/nav/Abc")
    assert r.status_code == 422
    assert "Navigate:" in r.text
    assert "not a valid topic prefix" in r.text
    # No corrected-form link block.
    assert "Corrected form:" not in r.text


def test_422_year_out_of_range_links_to_years(client: TestClient) -> None:
    """An out-of-range facet year 422 links to /years, naming the year."""
    for year in ("12345", "0", "99999"):
        r = client.get(f"/a/{TEST_TOKEN}/year/{year}")
        assert r.status_code == 422, year
        assert "Navigate:" in r.text
        assert f"/a/{TEST_TOKEN}/years" in r.text
        assert year in r.text


def test_422_bad_facet_letter_links_to_owning_index(client: TestClient) -> None:
    """A bad facet letter 422 links to /members or /bills, naming the letter."""
    r = client.get(f"/a/{TEST_TOKEN}/members/Z")
    assert r.status_code == 422
    assert f"/a/{TEST_TOKEN}/members" in r.text
    r2 = client.get(f"/a/{TEST_TOKEN}/bills/!x")
    assert r2.status_code == 422
    assert f"/a/{TEST_TOKEN}/bills" in r2.text


def test_422_search_bad_page_links_to_first_page(client: TestClient) -> None:
    """A bad page 422 links to the same search at page 1 (no page param)."""
    r = client.get(f"/a/{TEST_TOKEN}/search?q=data&page=-3")
    assert r.status_code == 422
    assert "Navigate:" in r.text
    assert f"/a/{TEST_TOKEN}/search?q=data" in r.text
    assert "not a valid page" in r.text


def test_422_search_bad_limit_builder() -> None:
    """A non-integer limit (rejected by FastAPI's type validation before the
    handler runs) still gets a corrected-form link: the search without the
    limit param (spec §6.3)."""
    correction, line = corrected_form_for_422(
        token=TEST_TOKEN, route="search", param="limit", value="abc",
        query="data")
    assert correction is not None
    assert f"/a/{TEST_TOKEN}/search?q=data" in correction
    assert "not a valid limit" in line


def test_422_search_bad_date_links_to_search_without_it(client: TestClient) -> None:
    """A bad from_/to date 422 reruns the search without the offending param."""
    r = client.get(f"/a/{TEST_TOKEN}/search?q=data&from_=not-a-date")
    assert r.status_code == 422
    assert f"/a/{TEST_TOKEN}/search?q=data" in r.text
    assert "not a valid date" in r.text


def test_corrected_form_builder_unknown_param(client: TestClient) -> None:
    """A 422 with no inferable correction (unknown param) carries the nav
    bar + plain-language line only — the complete spec §6.3 case set."""
    correction, line = corrected_form_for_422(
        token=TEST_TOKEN, route="search", param="bogus", value="x")
    assert correction is None
    assert line == ""
    # The malformed-report-id 422 (above) proves the nav-bar-only rendering.


# --- 429 / 502 / 503 retry guidance (spec §6.4) ---------------------------------


def test_502_search_retry_guidance(client: TestClient) -> None:
    """A 502 on /search carries the nav bar + retry guidance, NO Retry-After."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(status_code=502, json={"err": 1})
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            r = client.get(f"/a/{TEST_TOKEN}/search?q=probe")
        finally:
            pair.stop()
    assert r.status_code == 502
    assert "Navigate:" in r.text
    assert "temporary condition" in r.text
    assert "Retry-After" not in r.headers


def test_429_retry_guidance_no_retry_after() -> None:
    """A 429 (per-minute budget) carries the nav bar + retry guidance and NO
    Retry-After header (spec §6.4: only emit if honoured — we are not)."""
    import hansard_gateway.auth as auth_mod
    from tests.conftest import FakeClock

    low_budget = Settings(per_token_rate_per_min=2)
    app = create_app(app_settings=low_budget, index_override=None)
    auth_mod._store = _fresh_token_store()
    client = TestClient(app)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_empty_search(mock)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            resp = None
            for _ in range(4):
                resp = client.get(f"/a/{TEST_TOKEN}/search?q=probe")
                if resp.status_code == 429:
                    break
        finally:
            pair.stop()
    assert resp is not None and resp.status_code == 429
    assert "Navigate:" in resp.text
    assert "temporary condition" in resp.text
    assert "Retry-After" not in resp.headers


def _fresh_token_store() -> "object":
    """A single-token store for the low-budget 429 test (module-level app)."""
    import hashlib
    import tempfile
    from pathlib import Path

    from hansard_gateway.auth import TokenStore

    tmp = Path(tempfile.mkdtemp())
    entry = {
        "label": "hg-test",
        "sha256": hashlib.sha256(TEST_TOKEN.encode("utf-8")).hexdigest(),
        "enabled": True,
        "last4": "cdef",
    }
    path = tmp / "tokens.yaml"
    path.write_text(
        "tokens:\n"
        f"- label: {entry['label']}\n"
        f"  sha256: {entry['sha256']}\n"
        f"  enabled: true\n"
        f"  last4: {entry['last4']}\n",
        encoding="utf-8",
    )
    return TokenStore(path=path)


# --- upstream-failure resilience (spec §6.4) ------------------------------------


def test_ladder_and_facets_survive_upstream_502(client: TestClient) -> None:
    """With SPRS stubbed to 502: /search fails, but the index-only ladder +
    facet pages still serve (they never touch upstream)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(status_code=502, json={"err": 1})
        r_search = client.get(f"/a/{TEST_TOKEN}/search?q=probe")
        r_nav = client.get(f"/a/{TEST_TOKEN}/nav/a")
        r_years = client.get(f"/a/{TEST_TOKEN}/years")
        r_members = client.get(f"/a/{TEST_TOKEN}/members")
        r_launcher = client.get(f"/a/{TEST_TOKEN}/")
    assert r_search.status_code == 502
    assert "Navigate:" in r_search.text  # the 502 itself stays navigable
    for name, resp in (("nav", r_nav), ("years", r_years),
                       ("members", r_members), ("launcher", r_launcher)):
        assert resp.status_code == 200, name
        assert "Navigate:" in resp.text, name


# --- pagination / limit finished links (spec §6.5) --------------------------------


def test_search_offers_limit_links(client: TestClient) -> None:
    """Every /search offers limit=50/100/200 as finished links (the current
    50 is labelled; 100/200 link out)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_empty_search(mock)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            r = client.get(f"/a/{TEST_TOKEN}/search?q=Pension%20Fund")
        finally:
            pair.stop()
    assert r.status_code == 200
    assert "Page size" in r.text
    assert "Limit 50 per page (current)" in r.text
    # The rendered hrefs are HTML-escaped (&amp; for &).
    assert f"/a/{TEST_TOKEN}/search?q=Pension%20Fund&amp;limit=100" in r.text
    assert f"/a/{TEST_TOKEN}/search?q=Pension%20Fund&amp;limit=200" in r.text


def test_limit_links_builder_direct() -> None:
    """build_limit_links: three finished links; the current limit is labelled."""
    out = build_limit_links(
        token=TEST_TOKEN, query="data", page=2, current_limit=50
    )
    urls = [l["url"] for l in out]
    assert len(out) == 3
    assert urls[0].endswith("limit=50") and "current" in out[0]["label"]
    assert urls[1].endswith("limit=100")
    assert urls[2].endswith("limit=200")
    # page is preserved for the non-current offers, reset for the current one.
    assert "page=2" in urls[1] and "page=2" in urls[2]
    assert "page=" not in urls[0]


# --- the single most important invariant (spec §6) --------------------------------


def test_every_valid_token_error_carries_working_token_link(client: TestClient) -> None:
    """404/422/502 bodies (valid token) each contain >=1 absolute
    token-bearing link that returns 200 when followed."""
    import json as _json

    from hansard_gateway.sprs.client import _NO_RESULTS_MARKER

    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(
            status_code=500,
            content=_json.dumps({"resultHTML": _NO_RESULTS_MARKER}))
        cases = [("404", client.get(
            f"/a/{TEST_TOKEN}/report/bill-999").status_code)]
        mock.post("/searchResult").respond(status_code=502, json={"err": 1})
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            cases.append(("502", client.get(
                f"/a/{TEST_TOKEN}/search?q=probe").status_code))
        finally:
            pair.stop()
    cases.append(("422-nav", client.get(
        f"/a/{TEST_TOKEN}/nav/abcdef").status_code))
    cases.append(("422-year", client.get(
        f"/a/{TEST_TOKEN}/year/99999").status_code))
    cases.append(("422-search", client.get(
        f"/a/{TEST_TOKEN}/search?q=data&limit=abc").status_code))

    for name, status in cases:
        assert status in (404, 422, 502), name
    # Follow the nav-home link from each error page.
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(
            status_code=500,
            content=_json.dumps({"resultHTML": _NO_RESULTS_MARKER}))
        mock.post("/searchResult").respond(status_code=502, json={"err": 1})
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            r404 = client.get(f"/a/{TEST_TOKEN}/report/bill-999").text
            r502 = client.get(f"/a/{TEST_TOKEN}/search?q=probe").text
            r422a = client.get(f"/a/{TEST_TOKEN}/nav/abcdef").text
            r422b = client.get(f"/a/{TEST_TOKEN}/year/99999").text
            r422c = client.get(f"/a/{TEST_TOKEN}/search?q=data&limit=abc").text
        finally:
            pair.stop()
    home = f"{settings.public_base_url}/a/{TEST_TOKEN}/"
    for name, body in (("404", r404), ("502", r502),
                       ("422-nav", r422a), ("422-year", r422b),
                       ("422-search", r422c)):
        assert home in body, f"{name}: no absolute token-bearing home link"
    r_home = client.get(f"/a/{TEST_TOKEN}/")
    assert r_home.status_code == 200
