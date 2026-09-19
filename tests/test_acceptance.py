"""Offline acceptance matrix — spec §30 (A-I) + addendum §29/§33 (respx).

Every automated item of the release acceptance matrix is proven here with
TestClient + respx, no network. The live halves (raw curl over HTTPS, log
inspection on both hosts, upstream-request inspection, live Pair 201) are
manual deploy gates in deploy/RUNBOOK.md and the Plan 05 checkpoint — they are
NOT faked here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import hansard_gateway.auth as auth_mod
from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import settings
from hansard_gateway.main import create_app
from hansard_gateway.models import SearchHit, SearchPage
from hansard_gateway.render import render_search
from hansard_gateway.sprs.payload import topic_source_url

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"

#: Host of the gateway's own public origin (for the §33 same-origin exemption).
PUBLIC_BASE_HOST = settings.public_base_url.split("//", 1)[1].rstrip("/")

#: The E2E target report (spec §30-A).
E2E_REPORT_ID = "037_20041019_S0004_T0023"

#: The spec §30-A / §23 title, case-normalized. The upstream meta Title is
#: uppercase; the parser preserves it verbatim (spec §14), so the offline
#: matrix compares case-insensitively. The LIVE curl gate in the Plan 05
#: checkpoint greps the canonical mixed-case string against the deployed
#: endpoint (spec §30-A / §23, runbook step 3).
EXPECTED_TITLE = "singapore armed forces (amendment no. 2) bill"

#: The quoted-phrase probe for §30-D.
PHRASE_QUERY = "Consolidated Fund"

#: The unicode/encoding probe for §30-E (asserted escaped, not as a tag).
SCRIPT_PROBE = "<script>alert(1)</script>"

#: The §30-G transport-failure error marker (spec §17: no fabrication).
NO_FABRICATION_MARKER = "No transcript has been generated or substituted"

#: §33 attribute scan: any src=/href= whose value is an absolute http(s) URL.
_EXTERNAL_URL_ATTR = re.compile(r'(?:src|href)\s*=\s*["\']https?://', re.IGNORECASE)

#: §33 exemption: the provenance "Official SPRS record" outbound navigation link
#: (an <a> with rel="noopener noreferrer"), NOT a resource load. DOTALL: the
#: template emits these anchors across two lines (href line + rel line).
_PROVENANCE_ANCHOR = re.compile(
    r'<a\s+[^>]*href=["\']https?://[^"\']*["\'][^>]*rel=["\']noopener noreferrer["\']',
    re.IGNORECASE | re.DOTALL,
)

#: §33 exemption (self-URL change): an <a> pointing at the gateway's OWN origin
#: (absolute, token-preserving navigation links). Same-origin navigation is not a
#: third-party resource load. Excludes the provenance outbound anchors (handled
#: by _PROVENANCE_ANCHOR, a different host).
_SELF_ANCHOR = re.compile(
    r'<a\s+[^>]*href=["\']https?://' + re.escape(PUBLIC_BASE_HOST) + r'/',
    re.IGNORECASE | re.DOTALL,
)

#: A hit title present in the 2004 search fixture (first data row).
FIXTURE_HIT_TITLE = "INCOME TAX (AMENDMENT) BILL"

#: The date the §30-B/C rows sit on (sprs2 long form rendered by the template).
FIXTURE_SITTING_DATE = "2004-10-19"
FIXTURE_SITTING_DATE_LONG = "19 October 2004"

#: The §30-I fixture filenames (ground truth, spec §30-I).
FIXTURE_NAMES = (
    "topic_20041019_saf.json",
    "topic_20250108_bill742.json",
    "searchresult_20041019_p1.json",
    "searchresult_20250108_p1.json",
)

#: Minimum fixture size (bytes) — the ground-truth payloads are real upstream
#: captures, not stubs (spec §30-I: "representative upstream payloads").
_MIN_FIXTURE_BYTES = 100


@pytest.fixture()
def app(token_store):
    """The full app wired to the fixture token store."""
    auth_mod._store = token_store
    return create_app()


@pytest.fixture()
def client(app) -> TestClient:
    """A TestClient over the app."""
    return TestClient(app)


def _saf_topic(fixtures_dir: Path) -> dict[str, object]:
    """The 2004 SAF sprs2 topic payload (the tracer's E2E target)."""
    return json.loads(
        (fixtures_dir / "topic_20041019_saf.json").read_text(encoding="utf-8")
    )


def _search_rows(fixtures_dir: Path) -> list[dict]:
    """The 2004 searchResult rows (spec §30-B/C ground truth)."""
    return json.loads(
        (fixtures_dir / "searchresult_20041019_p1.json").read_text(encoding="utf-8")
    )


def _stub_search(mock: respx.MockRouter, rows: list[dict]) -> None:
    """Stub searchResult so every sweep fetch returns `rows` (saturates the sweep)."""
    mock.post("/searchResult").respond(json=rows)


def _stub_pair_degraded(mock: respx.MockRouter) -> None:
    """Stub the Pair upstream as degraded (200, not the spike's 201).

    The Pair provider takes its logged-unavailable path on any non-201 status
    (carry-forward #1 from Plan 04); this is the intended offline behaviour —
    the live 201 is asserted in the live-marked tests. Without a route, respx
    keeps assert_all_mocked=True and the un-mocked POST raises.
    """
    mock.post(f"{PAIR_BASE}/api/v1/search").respond(json={"searchResults": []})


def _synthetic_page(query: str) -> SearchPage:
    """A non-empty SearchPage for the §30-H / §33 synthetic search page."""
    hit = SearchHit(
        report_id="row-1#hansardContent-x#",
        link_id=E2E_REPORT_ID,
        date=None,
        title=FIXTURE_HIT_TITLE,
        report_type="bill",
        speaker=None,
        excerpt=None,
    )
    return SearchPage(query=query, total=2, page=1, limit=50, hits=[hit, hit])


def _search_page_html(query: str) -> str:
    """Render a synthetic search page through the real render layer."""
    return render_search(page=_synthetic_page(query), token=TEST_TOKEN)


def _matrix_pages(client: TestClient, fixtures_dir: Path) -> dict[str, str]:
    """Every 200-protected page the offline matrix exercises (name -> html).

    /report and /date use real fixture payloads (title/transcript present);
    /search uses a synthetic model page (link shape is what §30-H/§33 scan);
    /home is the authenticated home.
    """
    topic = _saf_topic(fixtures_dir)
    rows = _search_rows(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json={"resultHTML": topic})
        report = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}").text
        _stub_search(mock, rows)
        _stub_pair_degraded(mock)
        date_page = client.get(f"/a/{TEST_TOKEN}/date/{FIXTURE_SITTING_DATE}").text
        home = client.get(f"/a/{TEST_TOKEN}/").text
    return {
        "report": report,
        "date": date_page,
        "search": _search_page_html("Pension Fund"),
        "home": home,
    }


def test_html_no_js(client: TestClient, fixtures_dir: Path) -> None:
    """§30-H offline: no 200-protected page has <script>; content is static."""
    pages = _matrix_pages(client, fixtures_dir)
    for name, body in pages.items():
        assert "<script" not in body.lower(), f"{name}: <script> tag found"

    # Substantive content in the static body (no JS gating).
    assert EXPECTED_TITLE in pages["report"].lower()
    assert E2E_REPORT_ID in pages["report"]
    assert f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}" in pages["date"]
    assert f'href="https://{PUBLIC_BASE_HOST}/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}"' in pages["search"]
    # Phase 27.1 wave 2: the home page is the form-free departure board
    # (this offline matrix app has no index, so the board renders empty
    # sections — the token-preserving link shape is what matters here).
    assert f"/a/{TEST_TOKEN}/nav/" in pages["home"]
    assert f"/a/{TEST_TOKEN}/?format=json" in pages["home"]
    assert "<form" not in pages["home"].lower()


def test_no_third_party_resources(client: TestClient, fixtures_dir: Path) -> None:
    """§33 offline: no 200-protected page loads an external http(s) resource."""
    pages = _matrix_pages(client, fixtures_dir)
    for name, body in pages.items():
        external = [
            match.group(0)
            for match in _EXTERNAL_URL_ATTR.finditer(body)
            if not _is_exempt_provenance(body, match.start())
        ]
        assert not external, f"{name}: external resource load(s) {external}"
        # All CSS is inline/local: no stylesheet link element on any page.
        assert "<link" not in body.lower(), f"{name}: <link> (external stylesheet?) found"


def _is_exempt_provenance(body: str, start: int) -> bool:
    """True if the external URL at `start` is exempt (provenance or own origin)."""
    window = body[max(0, start - 12):start + 120]
    return bool(_PROVENANCE_ANCHOR.search(window)) or bool(
        _SELF_ANCHOR.search(window)
    )


# --- §30 A-G, I (matrix items) -------------------------------------------


def test_30a_report_200_with_transcript(client: TestClient, fixtures_dir: Path) -> None:
    """§30-A: 200; title, report id, date, provenance in the raw bytes."""
    fixture = _saf_topic(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json={"resultHTML": fixture})
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert resp.status_code == 200
    body = resp.text
    assert EXPECTED_TITLE in body.lower()
    assert E2E_REPORT_ID in body
    assert FIXTURE_SITTING_DATE_LONG in body
    assert "Singapore Parliamentary Reports" in body
    assert topic_source_url(
        report_id=E2E_REPORT_ID, sitting_date_iso=FIXTURE_SITTING_DATE
    ) in body


def test_30b_date_toc_has_saf_bill_link(
    client: TestClient, fixtures_dir: Path
) -> None:
    """§30-B: 200; the SAF bill's report link resolves to the gateway route."""
    rows = _search_rows(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock, rows)
        resp = client.get(f"/a/{TEST_TOKEN}/date/{FIXTURE_SITTING_DATE}")
    assert resp.status_code == 200
    assert f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}" in resp.text


def test_30c_search_hits_have_id_date_title_and_links(
    client: TestClient, fixtures_dir: Path
) -> None:
    """§30-C: 200; hits carry report id/date/title and clickable gateway links."""
    rows = _search_rows(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock, rows)
        _stub_pair_degraded(mock)
        resp = client.get(
            f"/a/{TEST_TOKEN}/search?q=Pension%20Fund"
            "&from=2004-01-01&to=2004-12-31"
        )
    assert resp.status_code == 200
    body = resp.text
    assert f"href=\"https://{PUBLIC_BASE_HOST}/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}\"" in body
    assert E2E_REPORT_ID in body
    assert FIXTURE_SITTING_DATE_LONG in body
    assert FIXTURE_HIT_TITLE in body


def test_30d_quoted_phrase_passed_to_upstream(
    client: TestClient, fixtures_dir: Path
) -> None:
    """§30-D: the quoted phrase passes through verbatim in the upstream body."""
    rows = _search_rows(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        route = mock.post("/searchResult").respond(json=rows)
        _stub_pair_degraded(mock)
        resp = client.get(f"/a/{TEST_TOKEN}/search?q=%22{PHRASE_QUERY}%22")
    assert resp.status_code == 200
    # The request body (json) carries the keyword verbatim, quotes included.
    body = json.loads(route.calls[0].request.content)
    assert body["keyword"] == f'"{PHRASE_QUERY}"'


def test_30e_query_displayed_escaped(client: TestClient) -> None:
    """§30-E: a <script> probe in q is displayed escaped; no raw tag, no 5xx."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(json=[])
        _stub_pair_degraded(mock)
        resp = client.get(f"/a/{TEST_TOKEN}/search?q={SCRIPT_PROBE}")
    assert resp.status_code == 200
    assert SCRIPT_PROBE not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_30f_invalid_id_no_arbitrary_fetch(client: TestClient) -> None:
    """§30-F: invalid/traversal ids → 404/422 with zero upstream POSTs."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        route = mock.post("/getHansardTopic")
        resp = client.get(f"/a/{TEST_TOKEN}/report/..%2f..%2fetc%2fpasswd")
    assert resp.status_code in (400, 404, 422)
    assert route.call_count == 0


def test_30g_upstream_unavailable_static_error(client: TestClient) -> None:
    """§30-G: transport failure → 502/503/504, static page, no fabricated text."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").mock(
            side_effect=httpx.ReadTimeout("read timed out")
        )
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert resp.status_code == settings.http_bad_gateway
    assert NO_FABRICATION_MARKER in resp.text
    assert EXPECTED_TITLE not in resp.text.lower()


def test_30i_parser_fixtures_delegate() -> None:
    """§30-I: the regression fixtures exist and are real payloads (not stubs).

    The parse assertions themselves live in test_sprs2_parser /
    test_sprs3_parser, which run offline against these same fixtures.
    """
    fixtures = Path(__file__).parent / "fixtures"
    for name in FIXTURE_NAMES:
        path = fixtures / name
        assert path.exists(), f"missing fixture {name}"
        assert len(path.read_text(encoding="utf-8")) > _MIN_FIXTURE_BYTES, (
            f"fixture {name} too small"
        )


# --- Addendum §29 (automated items) ---------------------------------------


def test_29_valid_token_200(client: TestClient, fixtures_dir: Path) -> None:
    """§29: a valid token yields 200 + the actual transcript."""
    fixture = _saf_topic(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json={"resultHTML": fixture})
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert resp.status_code == 200
    assert EXPECTED_TITLE in resp.text.lower()


def test_29_invalid_token_404_no_upstream(client: TestClient) -> None:
    """§29: an invalid token → 404 and NO upstream SPRS request."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        route = mock.post("/getHansardTopic")
        resp = client.get(f"/a/not-a-real-token/report/{E2E_REPORT_ID}")
    assert resp.status_code == settings.http_not_found
    assert route.call_count == 0


def test_29_revoked_token_404_others_work(client: TestClient, app) -> None:
    """§29: the revoked fixture token → 404 while the enabled one still works."""
    revoked = "hg_revokedtoken0123456789abcdef"
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        topic_route = mock.post("/getHansardTopic")
        denied = client.get(f"/a/{revoked}/report/{E2E_REPORT_ID}")
    assert denied.status_code == settings.http_not_found
    assert topic_route.call_count == 0
    fixture = _saf_topic(Path(__file__).parent / "fixtures")
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json={"resultHTML": fixture})
        allowed = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert allowed.status_code == 200


def test_29_token_preserving_search_links(client: TestClient, fixtures_dir: Path) -> None:
    """§29: every search result link keeps the /a/{token}/ prefix."""
    rows = _search_rows(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock, rows)
        _stub_pair_degraded(mock)
        resp = client.get(f"/a/{TEST_TOKEN}/search?q=Pension%20Fund")
    assert resp.status_code == 200
    assert 'href="/report/' not in resp.text
    assert f'href="https://{PUBLIC_BASE_HOST}/a/{TEST_TOKEN}/report/' in resp.text


def test_29_protected_response_headers(client: TestClient, fixtures_dir: Path) -> None:
    """§29: Referrer-Policy / Cache-Control / X-Robots-Tag on authenticated 200s."""
    fixture = _saf_topic(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json={"resultHTML": fixture})
        resp = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert resp.status_code == 200
    assert resp.headers.get("referrer-policy") == "no-referrer"
    assert resp.headers.get("cache-control") == "private, no-store"
    assert resp.headers.get("x-robots-tag") == "noindex, nofollow, noarchive"


def test_upstream_requests_carry_no_token(client: TestClient, fixtures_dir: Path) -> None:
    """Addendum §19/§29 offline half: no outgoing request header/body has the token."""
    fixture = _saf_topic(fixtures_dir)
    rows = _search_rows(fixtures_dir)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        topic = mock.post("/getHansardTopic").respond(json={"resultHTML": fixture})
        search = mock.post("/searchResult").respond(json=rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False)
        pair.post("/api/v1/search").respond(json={"searchResults": []})
        pair.start()
        try:
            client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
            client.get(f"/a/{TEST_TOKEN}/search?q=Pension%20Fund")
        finally:
            pair.stop()
    for route in (topic, search):
        for call in route.calls:
            assert TEST_TOKEN not in call.request.headers
            assert TEST_TOKEN not in call.request.content.decode("utf-8", "replace")
    for call in pair.calls:
        assert TEST_TOKEN not in call.request.content.decode("utf-8", "replace")
