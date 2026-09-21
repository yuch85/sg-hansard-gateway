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
_INVALID_404_MD5 = "db5bd37212a04ab36d3eb130cdc7abfd"
_INVALID_404_FIXTURE = Path(__file__).parent / "fixtures" / "invalid_token_404_body.html"

#: The spec §5.1 nav strip label (must occur twice: top and bottom).
_NAV_MARKER = "Navigate: Home"

#: spec §7.1 budgets.
PAGE_BUDGET_HTML_BYTES = 100 * 1024
PAGE_BUDGET_LINKS = 400
#: The Wave-2 (27.3-03) restyle raised the inline-CSS budget: the approved
#: a8 wireframe system (newspaper-warm palette, the 17rem sticky-TOC grid,
#: the sticky .sp-head persistent-speaker row, speaker classes, the print
#: block) measures ~4.9 KB even after the plan's presentational-only trims
#: (2 KB could not hold it — the deviation record in the 27.3-03-SUMMARY).
#: 8 KB is headroom over the measured 4928 B so Waves 3/4 restyles (search /
#: launcher / nav / date) have room without re-touching this constant.
PAGE_BUDGET_CSS_BYTES = 8 * 1024

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
        # Same-page in-page fragment anchors (the Wave-2 TOC #speech-N links,
        # and any future in-page navigation) are a third link class: they
        # carry no token (R3 applies to token-bearing links) and no .u twin
        # (R2's echo requirement applies to absolute token-bearing URLs — a
        # relative fragment has nothing new to echo; the full absolute Cite
        # URL for the same target already carries its .u twin on the Cite
        # line). The linter's 400-anchor cap (PAGE_BUDGET_LINKS) counts
        # them — they are anchors — but R1/R2/R3 are scoped to absolute
        # links only (27.3-03; the plan's TOC machine-surface truth).
        if href.startswith("#") and not href.startswith("#/"):
            continue
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
    (fixture + md5 689525ee…) and carries no nav strip (T-27.1-11)."""
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
    # Wave-2 restyle (27.3-03) minified the shared <style> block (no space
    # after the selector) — match both forms.
    u_rule = re.search(r"span\.u ?\{([^}]*)\}", base_html)
    assert u_rule, "base.html must style span.u"
    css = u_rule.group(1)
    for banned in ("display: none", "display:none", "visibility: hidden",
                   "visibility:hidden", "aria-hidden", "width: 0", "height: 0"):
        assert banned not in css, f".u must stay visible: {banned!r} found"


# --- Wave-1 (27.3-02) additions: cite cap, .u two-tier invariant, extraction --


#: The synthetic long-sitting speech count for the cite-cap conformance test
#: (G-A5-3: per-speech Cite lines on long sittings must stay within the caps).
SYNTHETIC_SPEECH_COUNT = 300


def _synthetic_long_report() -> Any:
    """A 300-speech HansardReport for the cap assertion (test-local dataclass
    construction, no fixture file — plan 27.3-02 Task 3)."""
    from datetime import date

    from hansard_gateway.models import HansardReport, Speech

    return HansardReport(
        report_id="synth-300",
        date=date(2020, 1, 1),
        title="Synthetic Long Sitting",
        topic_type=None,
        source_url="https://sprs.parl.gov.sg/search/#/topic?reportid=synth-300",
        volume=None,
        parliament_no=None,
        session_no=None,
        sitting_no=None,
        speeches=[
            Speech(
                sequence=i + 1,
                speaker_original=(
                    None if i % 7 == 0 else f"Member {i} (PPM)"
                ),
                speaker_name=None,
                speaker_role=None,
                paragraphs=[f"Paragraph {i} of the synthetic sitting."],
            )
            for i in range(SYNTHETIC_SPEECH_COUNT)
        ],
        transcript_sha256="synthetic",
    )


def _render_report_direct(report: Any, *, token: str) -> str:
    """Render one report straight through the render layer (no HTTP)."""
    from hansard_gateway.render import render_report

    return render_report(
        report=report, token=token, retrieved="2020-01-01T00:00:00Z"
    )


def test_cite_cap_conformance() -> None:
    """The cap rule holds numerically on a 300-speech synthetic sitting.

    Renders the report through the real render layer, then asserts:
    (a) the link cap holds (≤400) — the byte cap is verified as an
        ESTIMATE-BRANCH invariant below (the pre-render estimate is a
        conservative upper bound: a synthetic 300-speech page with 170 Cite
        lines + ~40 KB of body text legitimately exceeds 100 KB on the body
        alone, so the byte cap is asserted on the estimate branch, not on the
        synthetic's measured bytes);
    (b) the rendered Cite-line count == cite_speech_limit(...) recomputed with
        the SAME inputs (the cap function, imported, not re-implemented —
        G-A6-2 determinism);
    (c) every rendered Cite line's href carries the request token (R3) and has
        a .u twin (R2);
    (d) speeches past the cap have NO Cite line but DO keep id="speech-N"
        (ids are uncapped — the cap limits rendered Cite LINKS only).
    """
    from hansard_gateway.render.cite import (
        build_cite_context,
        cite_speech_limit,
    )

    report = _synthetic_long_report()
    body = _render_report_direct(report, token=TEST_TOKEN)

    # (a) the link cap.
    parser = _AnchorParser()
    parser.feed(body)
    assert len(parser.anchors) <= PAGE_BUDGET_LINKS, (
        f"synthetic page over link budget: {len(parser.anchors)}"
    )

    # (b) rendered Cite count == the cap function's own output. Recompute
    # with the same inputs the render layer used: non-Cite links = total
    # anchors minus the rendered Cite lines; base_bytes = page size minus the
    # rendered Cite-line bytes (each Cite line contributes its anchor + twin).
    # Count Cite LINES precisely: each is <p class="cite">Cite: <a …
    # (a bare class="cite" count would also match .cite-note / .cite a
    # occurrences, and the linter's own cap counts <a> anchors, not <p>).
    cite_count = len(re.findall(r'<p class="cite">Cite: <a ', body))
    # (b) determinism (G-A6-2): the rendered Cite count == the cap function's
    # output at the SAME inputs the render layer used. The render layer
    # measures the PRE-CITE page (measure_non_cite_page: the page's absolute
    # link count + byte size, without Cite lines). Recompute those exact
    # inputs from the final page: strip the Cite lines + the cite-note
    # (rendered only when a Cite line exists), then count the absolute
    # (https) links — ALL of them, token-bearing or not (the linter's own
    # §7.1 link cap counts every anchor, and the 2 SPRS provenance links are
    # absolute https). Each Cite line adds exactly one absolute anchor.
    cite_line_re = re.compile(r"\s*<p class=\"cite\">.*?</p>", re.DOTALL)
    pre_cite = cite_line_re.sub("", body).replace(
        '<p class="cite-note">To cite a specific speech, copy its Cite URL — '
        "opening it in a browser highlights that speech.</p>\n    ",
        "",
    )
    pre_cite_parser = _AnchorParser()
    pre_cite_parser.feed(pre_cite)
    # The render layer's measurement (measure_non_cite_page) counts the
    # page's TOKEN-BEARING absolute links (B in the cap rule) — not the
    # 2 external SPRS provenance links. Count the same set:
    non_cite_links = len([
        a for a in pre_cite_parser.anchors
        if urlsplit(a["href"]).scheme == "https"
        and f"/a/{TEST_TOKEN}/" in a["href"]
    ])
    base_bytes = len(pre_cite.encode("utf-8"))
    from hansard_gateway.render.cite import CITE_EST_BYTES_PER_LINE

    expected = cite_speech_limit(
        speech_count=SYNTHETIC_SPEECH_COUNT,
        non_cite_links=non_cite_links,
        base_bytes=base_bytes,
        est_bytes_per_cite=CITE_EST_BYTES_PER_LINE,
    )
    assert cite_count == expected, (
        f"rendered Cite lines {cite_count} != cap function {expected} "
        f"(non_cite_links={non_cite_links}, base_bytes={base_bytes}, "
        f"est={CITE_EST_BYTES_PER_LINE})"
    )

    # (c) every Cite line: token in href (R3) + .u twin (R2).
    cite_hrefs = re.findall(r'<p class="cite">.*?href="([^"]+)"', body)
    for href in cite_hrefs:
        assert f"/a/{TEST_TOKEN}/" in href, f"Cite href missing token: {href}"
        assert href in parser.twins, f"Cite href missing .u twin: {href}"

    # (d) uncapped ids: every speech keeps id="speech-N" whether or not cited.
    for n in range(1, SYNTHETIC_SPEECH_COUNT + 1):
        assert f'id="speech-{n}"' in body, f"missing id speech-{n}"
    # And the past-cap speeches really have no Cite line.
    assert cite_count < SYNTHETIC_SPEECH_COUNT, (
        "cap should bind on a 300-speech sitting"
    )
    # Cross-check with build_cite_context (the context builder's own None
    # placement matches the rendered Cite count).
    urls, _labels = build_cite_context(
        report=report, token=TEST_TOKEN,
        non_cite_links=non_cite_links, base_bytes=base_bytes,
    )
    assert sum(1 for u in urls if u is not None) == cite_count

    # (e) the byte branch of the cap is a real bound: the cap function must
    # reduce the limit when the estimated Cite bytes exceed the headroom.
    # (The synthetic page's MEASURED bytes can exceed 100 KB on body text
    # alone — the byte cap is enforced pre-render by the ESTIMATE branch, and
    # post-render by the linter on real pages.)
    from hansard_gateway.render.cite import CITE_EST_BYTES_PER_LINE

    tight_base = PAGE_BUDGET_HTML_BYTES - CITE_EST_BYTES_PER_LINE * 2
    tight_limit = cite_speech_limit(
        speech_count=SYNTHETIC_SPEECH_COUNT,
        non_cite_links=non_cite_links,
        base_bytes=tight_base,
    )
    assert tight_limit == 2, (
        f"byte branch should bind at base={tight_base}: got {tight_limit}"
    )
    assert tight_limit < expected, "byte branch must reduce the limit"


def _style_blocks(html: str) -> list[str]:
    """All inline <style> block contents of one rendered page."""
    return re.findall(r"<style>(.*?)</style>", html, re.DOTALL)


#: Hiding declarations that must never appear on .u or an ancestor (a6
#: amendment 2 — the static tier).
_HIDDING_PATTERNS: tuple[str, ...] = (
    "display: none", "display:none",
    "visibility: hidden", "visibility:hidden",
    "opacity: 0;", "opacity:0;",
    "font-size: 0;", "font-size:0;",
    "width: 0;", "width:0;",
    "height: 0;", "height:0;",
    "aria-hidden",
)


def _rule_applies_to_u(selector: str) -> bool:
    """Whether a CSS selector targets .u or an ancestor element of .u."""
    selector = selector.strip()
    if ".u" in selector:
        return True
    for ancestor in ("body", "main", "article", "section", "footer", "nav"):
        if selector == ancestor or selector.startswith(ancestor + " ") \
                or selector.startswith(ancestor + ","):
            return True
    return False


def _no_hiding_on_u(css_text: str) -> list[str]:
    """Return violations: hiding declarations on .u or an ancestor selector.

    Selector-scoped (not a global grep): each { } rule's selector is checked
    against the .u/ancestor set, so a rule hiding, e.g., nav.toc in a print
    context is legal while hiding span.u (or body/main/…) is not.
    """
    violations: list[str] = []
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", css_text):
        selector, declarations = match.group(1).strip(), match.group(2)
        if not _rule_applies_to_u(selector):
            continue
        for banned in _HIDDING_PATTERNS:
            if banned in declarations:
                violations.append(
                    f"hiding declaration {banned!r} on {selector!r}"
                )
    return violations


def _rendered_corpus_bodies(client: TestClient) -> dict[str, str]:
    """Render every corpus page type offline; return {name: body}."""
    bodies: dict[str, str] = {}
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        _stub_search(mock)
        mock.post("/getHansardTopic").respond(json=_topic_fixture())
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            bodies["report"] = (
                client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}").text
            )
            bodies["search"] = (
                client.get(f"/a/{TEST_TOKEN}/search?q=Pension%20Fund").text
            )
            bodies["date"] = (
                client.get(f"/a/{TEST_TOKEN}/date/2004-10-19").text
            )
        finally:
            pair.stop()
    bodies["launcher"] = client.get(f"/a/{TEST_TOKEN}/").text
    bodies["nav_a"] = client.get(f"/a/{TEST_TOKEN}/nav/a").text
    bodies["years"] = client.get(f"/a/{TEST_TOKEN}/years").text
    bodies["members"] = client.get(f"/a/{TEST_TOKEN}/members").text
    bodies["bills"] = client.get(f"/a/{TEST_TOKEN}/bills").text
    # 422 error page (valid token).
    bodies["error_422"] = client.get(f"/a/{TEST_TOKEN}/report/bad id").text
    return bodies


def test_u_not_hidden_static_all_pages(client_with_index: TestClient) -> None:
    """STATIC tier (a6 amendment 2): no hiding declaration on .u or any
    ancestor in the rendered output of EVERY corpus page type (screen CSS —
    the print stylesheet is Wave 2/Plan 03, and this check is selector-scoped
    so a future print rule hiding nav chrome stays legal)."""
    for name, body in _rendered_corpus_bodies(client_with_index).items():
        for css in _style_blocks(body):
            violations = _no_hiding_on_u(css)
            assert not violations, (
                f"{name}: .u or an ancestor is hidden: {violations}"
            )


def test_u_rendered_extraction_all_pages(client_with_index: TestClient) -> None:
    """RENDERED extraction tier (a6 amendment 2, the load-bearing one a grep
    cannot provide): every absolute token-bearing href's URL survives a
    representative text-extraction path (strip <style>/<script>, remove tags,
    unescape) — i.e. its .u twin is extractable — on every corpus page type."""
    from html import unescape

    for name, body in _rendered_corpus_bodies(client_with_index).items():
        # The representative extraction: strip style/script, remove tags,
        # unescape entities, collapse whitespace.
        stripped = re.sub(
            r"<(style|script)[^>]*>.*?</\1>", "", body,
            flags=re.DOTALL,
        )
        text = unescape(re.sub(r"<[^>]+>", " ", stripped))
        text = re.sub(r"\s+", " ", text)
        parser = _AnchorParser()
        parser.feed(body)
        abs_token = [
            a["href"] for a in parser.anchors
            if a["rel"] != "noopener noreferrer"
            and urlsplit(a["href"]).scheme == "https"
            and f"/a/{TEST_TOKEN}/" in a["href"]
        ]
        assert abs_token, f"{name}: no absolute token hrefs found"
        missing = [h for h in abs_token if h not in text]
        assert not missing, (
            f"{name}: {len(missing)} absolute href URLs absent from the "
            f"extracted text (.u twin not extractable): {missing[:3]}"
        )


def test_u_single_line_not_clipped_narrow(client_with_index: TestClient) -> None:
    """F4 rendered gate (copilot pass-1 Major 1): the narrow-query .u rule
    keeps the URL DOM-preserved AND visually reachable — the mechanism must be
    a one-line scroll container, never a clip.

    (a) DOM-preserved: every span.u's full URL text is present in the
        rendered report page (existing strip-tags/un-escape extraction).
    (b) Not visually clipped: the narrow-query (max-width:62rem) .u rule
        declares white-space:nowrap and either overflow-x:auto (scroll) or
        no overflow at all — and contains NO overflow:hidden (a clip).
    """
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_topic_fixture())
        pair = respx.mock(base_url=PAIR_BASE, assert_all_called=False,
                          assert_all_mocked=False)
        pair.start()
        try:
            body = client_with_index.get(
                f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}"
            ).text
        finally:
            pair.stop()

    # (a) DOM-preserved: extraction path finds every .u URL in the DOM.
    stripped = re.sub(
        r"<(style|script)[^>]*>.*?</\1>", "", body, flags=re.DOTALL
    )
    from html import unescape
    text = unescape(re.sub(r"<[^>]+>", " ", stripped))
    text = re.sub(r"\s+", " ", text)
    u_urls = re.findall(
        r'<span class="u"[^>]*>(.*?)</span>', body, re.DOTALL
    )
    assert u_urls, "report page rendered no span.u echoes"
    missing = [u for u in u_urls if u not in unescape(text)]
    assert not missing, f"span.u URLs missing from DOM extraction: {missing[:3]}"

    # (b) The narrow-query .u MECHANISM rule (the one declaring
    # white-space:nowrap) must not be a clip. The F2 font-size floor rule on
    # .u is a separate concern and is excluded here.
    narrow_rules: list[str] = []
    for css in _style_blocks(body):
        for media in re.finditer(r"@media[^{]*62rem[^{]*\{", css):
            depth = 1
            pos = media.end()
            while pos < len(css) and depth:
                if css[pos] == "{":
                    depth += 1
                elif css[pos] == "}":
                    depth -= 1
                pos += 1
            # Strip CSS comments — comment text can contain braces or
            # selector-like tokens (e.g. "14px") that would pollute the scan.
            media_body = re.sub(
                r"/\*.*?\*/", "", css[media.end():pos - 1], flags=re.DOTALL
            )
            for rule in re.finditer(r"([^{}]+)\{([^}]*)\}", media_body):
                selector, declarations = rule.group(1).strip(), rule.group(2)
                if ".u" in selector and "white-space" in declarations:
                    narrow_rules.append(declarations)
    assert narrow_rules, "no narrow-query (max-width:62rem) .u rule found"
    for decls in narrow_rules:
        assert "white-space:nowrap" in decls, (
            f"narrow .u rule lacks white-space:nowrap: {decls}"
        )
        assert "overflow:hidden" not in decls.replace(" ", ""), (
            f"narrow .u rule visually clips (overflow:hidden): {decls}"
        )


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
    assert "Official SPRS record" in r.text
    # Mission 008: the provenance URL points at the SECTION route
    assert "#/topic?reportid=" in r.text


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
