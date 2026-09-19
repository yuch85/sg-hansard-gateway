"""R1–R9 link-rendering conformance linter (spec §5, Phase 27.1 wave 3).

A reusable pytest linter over rendered HTML bodies: parses anchors with the
stdlib HTMLParser and asserts per-anchor R1 (absolute, scheme+host, token in
path), R2 (visible <span class="u"> twin carrying the same URL), R3 (the
request token only), R5 (query percent-encoding round-trips), R6 (descriptive
anchor text), plus per-page R4 (no <script>/<form>/<iframe>, no off-host
stylesheet), the spec §5.1 nav strip at top AND bottom, and the §7.1 page
budgets. Wave 5 reuses these assertions as the §8.3 acceptance crawler.

Offline: TestClient + respx stubs; no real upstream call.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import respx
from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import settings

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"

#: The live production invalid-token 404 body fingerprint (no enumeration) —
#: pinned by tests/fixtures/invalid_token_404_body.html (the captured live body).
_BOGUS_TOKEN = "hg_invalidtoken00000000000000zz"
_INVALID_404_MD5 = "1a29cc1330d50031993c3cbcde2318d7"
_INVALID_404_FIXTURE = Path(__file__).parent / "fixtures" / "invalid_token_404_body.html"

#: The spec §5.1 nav strip label (must occur twice: top and bottom).
_NAV_MARKER = "Navigate: Home"

#: spec §7.1 budgets.
PAGE_BUDGET_HTML_BYTES = 100 * 1024
PAGE_BUDGET_LINKS = 400
PAGE_BUDGET_CSS_BYTES = 2 * 1024

#: A bare report-id anchor text (spec-style or live id) violates R6.
_BARE_ID_RE = re.compile(
    r"^(?:\d+_S\d+_T\d+|bill-\d+|\d{1,6}-[A-Z]{2}\.\d+(?:-[A-Z]{2})?#.*)$"
)

#: R6 target texts: non-descriptive boilerplate (spec §5 R6 — "click here",
#: "[1]" and the like). Single letters/digits are exempt: the launcher's
#: Find-a-topic and the A–Z facet index anchors are inherently one-char.
_NON_DESCRIBITIVE: frozenset[str] = frozenset(
    {"click here", "here", "link", "more", "read more", "click", "view"}
)


def _r6_violation(text: str) -> Optional[str]:
    """Return an R6 detail string when ``text`` is non-descriptive, else None."""
    stripped = text.strip()
    if not stripped:
        return "anchor text empty"
    if _BARE_ID_RE.match(stripped):
        return f"anchor text is a bare id: {text!r}"
    if stripped.lower() in _NON_DESCRIBITIVE:
        return f"non-descriptive anchor text: {text!r}"
    if re.fullmatch(r"[\[\(]\d+[\]\)]", stripped):
        return f"anchor text is a bare list marker: {text!r}"
    return None


class _AnchorParser(HTMLParser):
    """Collects anchors (href + text), .u twin spans, and forbidden tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict[str, str]] = []
        self._in_a = False
        self._a_href = ""
        self._a_rel = ""
        self._a_text = ""
        self.twins: list[str] = []
        self._in_u = False
        self._u_text = ""
        self.forbidden: list[str] = []
        self.stylesheet_hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        d = dict(attrs)
        if tag == "a":
            self._in_a = True
            self._a_href = d.get("href", "")
            self._a_rel = d.get("rel", "")
            self._a_text = ""
        elif tag == "span" and d.get("class") == "u":
            self._in_u = True
            self._u_text = ""
        elif tag in ("script", "form", "iframe"):
            self.forbidden.append(tag)
        elif tag == "link" and d.get("rel") == "stylesheet":
            self.stylesheet_hrefs.append(d.get("href", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_a:
            self.anchors.append(
                {"href": self._a_href, "text": self._a_text.strip(),
                 "rel": self._a_rel}
            )
            self._in_a = False
        elif tag == "span" and self._in_u:
            self.twins.append(self._u_text.strip())
            self._in_u = False

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # Self-closed <a/> forms (rare but legal) still count.
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._a_text += data
        if self._in_u:
            self._u_text += data


def _lint_anchors(
    body: str, *, token: str, base: str
) -> list[tuple[int, str, str]]:
    """Assert R1/R2/R3/R5/R6 on every anchor; return (idx, rule, detail)
    violations (empty list = conformant)."""
    parser = _AnchorParser()
    parser.feed(body)
    violations: list[tuple[int, str, str]] = []
    base = base.rstrip("/")
    # R4 carves out ONE external URL: the SPRS provenance link in a report
    # footer (rel="noopener noreferrer"). All other anchors must be
    # absolute, on the public base, and token-bearing.
    for i, a in enumerate(parser.anchors):
        href = a["href"]
        parts = urlsplit(href)
        if a["rel"] == "noopener noreferrer" and href.startswith("https://"):
            continue
        if not (parts.scheme == "https" and parts.netloc):
            violations.append((i, "R1", f"not absolute: {href!r}"))
            continue
        if not href.startswith(base + "/a/"):
            violations.append((i, "R1", f"not on public base: {href!r}"))
        if f"/a/{token}/" not in href:
            violations.append((i, "R3", f"missing request token: {href!r}"))
        if "/a/hg_" in href and f"/a/{token}/" not in href:
            violations.append((i, "R3", f"foreign token: {href!r}"))
        if href in parser.twins:
            parser.twins.remove(href)
        else:
            violations.append((i, "R2", f"no visible .u twin: {href!r}"))
        # R5: the query must round-trip — every value is the quote() of
        # itself (quote(v, safe='') is idempotent; '+' and '%20' are both
        # legal space encodings, so spaces are never flagged).
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if unquote(value) != value:
                violations.append(
                    (i, "R5", f"query value does not round-trip: {key}={value!r}")
                )
            if "%" not in value and any(
                c in value for c in "&#%\"'<>"
            ):
                violations.append(
                    (i, "R5", f"unencoded special char in {key}={value!r}")
                )
        r6 = _r6_violation(a["text"])
        if r6:
            violations.append((i, "R6", r6))
    return violations


def _lint_page(
    body: str, *, token: str, base: str, nav: bool = True
) -> list[str]:
    """Assert R4 + nav strip (top+bottom) + budgets on one HTML page."""
    problems: list[str] = []
    parser = _AnchorParser()
    parser.feed(body)
    if parser.forbidden:
        problems.append(f"R4 forbidden tags: {parser.forbidden}")
    offhost = [
        h for h in parser.stylesheet_hrefs
        if h.startswith("http") and not h.startswith(base)
    ]
    if offhost:
        problems.append(f"R4 off-host stylesheets: {offhost}")
    if nav:
        # Count the label word, not the full 'Navigate: Home' line: Jinja's
        # trim_blocks/lstrip_blocks eat the space before the {{ }} on the same
        # line as the literal (T-27-31), so the rendered text is 'Navigate:Home'
        # at one of the two occurrences.
        count = body.count("Navigate:")
        if count != 2:
            problems.append(f"nav strip must appear twice (top+bottom): {count}")
    if len(body.encode("utf-8")) > PAGE_BUDGET_HTML_BYTES:
        problems.append(f"page over HTML budget: {len(body)} > {PAGE_BUDGET_HTML_BYTES}")
    if len(parser.anchors) > PAGE_BUDGET_LINKS:
        problems.append(f"page over link budget: {len(parser.anchors)}")
    css = "\n".join(re.findall(r"<style>(.*?)</style>", body, re.DOTALL))
    if len(css.encode("utf-8")) > PAGE_BUDGET_CSS_BYTES:
        problems.append(f"inline CSS over budget: {len(css)} > {PAGE_BUDGET_CSS_BYTES}")
    return problems


def _lint_html(
    body: str, *, token: str, base: str, nav: bool = True
) -> list[str]:
    """Run the full linter on one protected HTML page; return problems."""
    anchor_violations = _lint_anchors(body, token=token, base=base)
    return (
        [f"anchor[{i}] {rule}: {detail}" for i, rule, detail in anchor_violations]
        + _lint_page(body, token=token, base=base, nav=nav)
    )


def _assert_conformant(name: str, body: str, *, token: str) -> None:
    base = settings.public_base_url.rstrip("/")
    problems = _lint_html(body, token=token, base=base)
    assert not problems, f"{name} conformance failures:\n" + "\n".join(problems)


# --- fixtures ----------------------------------------------------------------


def _search_fixture_rows() -> list[dict[str, Any]]:
    """The committed 2004 searchResult rows (offline ground truth)."""
    path = Path(__file__).parent / "fixtures" / "searchresult_20041019_p1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _topic_fixture() -> dict[str, Any]:
    """The committed 2004 sprs2 topic payload (the E2E report)."""
    path = Path(__file__).parent / "fixtures" / "topic_20041019_saf.json"
    return json.loads(path.read_text(encoding="utf-8"))


E2E_REPORT_ID = "037_20041019_S0004_T0023"


def _stub_search(mock: respx.MockRouter) -> None:
    mock.post("/searchResult").respond(json=_search_fixture_rows())


def test_launcher_conformant(client_with_index: TestClient) -> None:
    r = client_with_index.get(f"/a/{TEST_TOKEN}/")
    assert r.status_code == 200
    _assert_conformant("launcher", r.text, token=TEST_TOKEN)


def test_nav_ladder_conformant(client_with_index: TestClient) -> None:
    r = client_with_index.get(f"/a/{TEST_TOKEN}/nav/a")
    assert r.status_code == 200
    _assert_conformant("nav/a", r.text, token=TEST_TOKEN)


def test_facet_pages_conformant(client_with_index: TestClient) -> None:
    for path in ("/years", "/members", "/bills"):
        r = client_with_index.get(f"/a/{TEST_TOKEN}{path}")
        assert r.status_code == 200, path
        _assert_conformant(f"facet {path}", r.text, token=TEST_TOKEN)


def test_search_conformant(client_with_index: TestClient) -> None:
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            r = client_with_index.get(f"/a/{TEST_TOKEN}/search?q=Pension%20Fund")
        finally:
            pair.stop()
    assert r.status_code == 200
    _assert_conformant("search", r.text, token=TEST_TOKEN)


def test_date_conformant(client_with_index: TestClient) -> None:
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock)
        r = client_with_index.get(f"/a/{TEST_TOKEN}/date/2004-10-19")
    assert r.status_code == 200
    _assert_conformant("date", r.text, token=TEST_TOKEN)


def test_report_conformant(client_with_index: TestClient) -> None:
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_topic_fixture())
        r = client_with_index.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert r.status_code == 200
    _assert_conformant("report", r.text, token=TEST_TOKEN)


def test_error_pages_conformant(token_store, index) -> None:
    """Valid-token error shapes (422/429/502) carry the nav strip + R anchors.

    Each shape gets its own fresh app/client: the rate budget is per-process,
    and the 429 shape uses a low-budget Settings override (the default is
    300/min — exhausting it would need 300 requests).
    """
    import hansard_gateway.auth as auth_mod
    from hansard_gateway.config import Settings
    from hansard_gateway.main import create_app

    auth_mod._store = token_store

    def _fresh(app_settings: Optional[Settings] = None) -> TestClient:
        return TestClient(create_app(
            app_settings=app_settings, index_override=index
        ))

    base = settings.public_base_url.rstrip("/")

    # 422: malformed report id (valid token).
    r = _fresh().get(f"/a/{TEST_TOKEN}/report/bad id")
    assert r.status_code == 422
    _assert_conformant("422", r.text, token=TEST_TOKEN)

    # 429: a 2-per-minute budget trips on the third gated (search) request.
    low_budget = Settings(per_token_rate_per_min=2)
    client = _fresh(low_budget)
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            resp = None
            for _ in range(4):
                resp = client.get(f"/a/{TEST_TOKEN}/search?q=probe")
                if resp.status_code in (429, 503):
                    break
        finally:
            pair.stop()
    assert resp is not None and resp.status_code in (429, 503)
    _assert_conformant("429", resp.text, token=TEST_TOKEN)

    # 502: upstream failure on a valid id.
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").mock(
            side_effect=__import__("httpx").ReadTimeout("timed out")
        )
        r = _fresh().get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert r.status_code in (502, 503)
    _assert_conformant("502", r.text, token=TEST_TOKEN)

    # The 502 page offers at least one absolute token-bearing link (R8).
    assert f"{base}/a/{TEST_TOKEN}/" in r.text


def test_invalid_token_404_byte_identical(client_with_index: TestClient) -> None:
    """The tokenless 404 stays byte-identical to the captured production body
    (fixture + md5 1a29cc13…) and carries no nav strip (T-27.1-11)."""
    fixture = _INVALID_404_FIXTURE.read_bytes()
    assert hashlib.md5(fixture).hexdigest() == _INVALID_404_MD5
    r = client_with_index.get(f"/a/{_BOGUS_TOKEN}/report/{E2E_REPORT_ID}")
    assert r.status_code == 404
    assert r.content == fixture, "tokenless 404 drifted from the production body"
    assert "Navigate:" not in r.text
    assert f"/a/{TEST_TOKEN}/" not in r.text


def test_u_span_css_is_visible() -> None:
    """Source assertion: base.html styles .u small/grey but never hides it."""
    base_html = (
        Path(__file__).parent.parent
        / "src" / "hansard_gateway" / "render" / "templates" / "base.html"
    ).read_text(encoding="utf-8")
    u_rule = re.search(r"span\.u \{([^}]*)\}", base_html)
    assert u_rule, "base.html must style span.u"
    css = u_rule.group(1)
    for banned in ("display: none", "display:none", "visibility: hidden",
                   "visibility:hidden", "aria-hidden", "width: 0", "height: 0"):
        assert banned not in css, f".u must stay visible: {banned!r} found"


def test_search_result_link_ids_percent_encoded(client_with_index: TestClient) -> None:
    """A link_id containing '#' renders quote()d and round-trips (R5)."""
    rows = _search_fixture_rows()
    rows[0]["linkId"] = "bill-742#frag"
    rows[0]["htmlFileName"] = None
    rows[0]["reportId"] = "bill-742#frag"
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/searchResult").respond(json=rows)
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            r = client_with_index.get(f"/a/{TEST_TOKEN}/search?q=Fund")
        finally:
            pair.stop()
    assert r.status_code == 200
    assert f"/a/{TEST_TOKEN}/report/bill-742%23frag" in r.text
    assert "report/bill-742#frag\"" not in r.text
    _assert_conformant("encoded search", r.text, token=TEST_TOKEN)


def test_report_sha_footer_and_provenance(client_with_index: TestClient) -> None:
    """The SHA-256 footer and the SPRS provenance link (rel noopener) survive."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_topic_fixture())
        r = client_with_index.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert r.status_code == 200
    assert "Transcript SHA-256" in r.text
    assert 'rel="noopener noreferrer"' in r.text
    assert "Official SPRS sitting record" in r.text


def test_cache_split_headers(client_with_index: TestClient) -> None:
    """Index-only pages max-age=300; content routes keep no-store (wave-2 OQ1)."""
    index_paths = [f"/a/{TEST_TOKEN}/", f"/a/{TEST_TOKEN}/nav/a",
                   f"/a/{TEST_TOKEN}/years"]
    for path in index_paths:
        r = client_with_index.get(path)
        assert r.headers["cache-control"] == "private, max-age=300", path
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock)
        r = client_with_index.get(f"/a/{TEST_TOKEN}/date/2004-10-19")
    assert "no-store" in r.headers["cache-control"]
