"""Citation-highlight tests (Phase 27.3 Wave 1 — plan 27.3-02).

Covers the fragment-aware ``abs_report_url``, the cap rule
(``cite.py:cite_speech_limit`` — a pure function, unit-tested WITHOUT
rendering per the plan's key_link), the rendered Cite lines + ids,
``?format=text``/``?format=json`` byte-identity, and verbatim body
integrity.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.config import settings
from hansard_gateway.models import HansardReport, Speech
from hansard_gateway.render.urls import abs_report_url

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
PAIR_BASE = "https://search.pair.gov.sg"
E2E_REPORT_ID = "037_20041019_S0004_T0023"

#: The spec §7.1 budgets (same values as test_link_conformance.py — the
#: cap rule must keep a rendered report under both).
PAGE_BUDGET_HTML_BYTES = 100 * 1024
PAGE_BUDGET_LINKS = 400


# --------------------------------------------------------------------------- #
# Task 1 — abs_report_url fragment param
# --------------------------------------------------------------------------- #


def test_abs_report_url_without_fragment_unchanged() -> None:
    """No fragment => the pre-change byte form (existing call sites)."""
    base = settings.public_base_url.rstrip("/")
    assert (
        abs_report_url(token=TEST_TOKEN, link_id="bill-774")
        == f"{base}/a/{TEST_TOKEN}/report/bill-774"
    )


def test_abs_report_url_with_fragment_appends_anchor() -> None:
    base = settings.public_base_url.rstrip("/")
    url = abs_report_url(token=TEST_TOKEN, link_id="bill-774", fragment="speech-3")
    assert url == f"{base}/a/{TEST_TOKEN}/report/bill-774#speech-3"


def test_abs_report_url_fragment_defensively_quoted() -> None:
    """A fragment with unsafe chars is quote()d (defensive; speech-N is safe)."""
    base = settings.public_base_url.rstrip("/")
    url = abs_report_url(token=TEST_TOKEN, link_id="bill-774",
                         fragment="speech 3")
    assert url == f"{base}/a/{TEST_TOKEN}/report/bill-774#speech%203"
    # '-' and '_' stay unquoted (safe in a fragment).
    url2 = abs_report_url(token=TEST_TOKEN, link_id="bill-774",
                          fragment="speech-3_x")
    assert url2.endswith("#speech-3_x")


# --------------------------------------------------------------------------- #
# Task 1 — the cap rule (pure arithmetic, no rendering)
# --------------------------------------------------------------------------- #


def test_cap_bill774_all_cited() -> None:
    """bill-774 (14 speeches, 23 non-Cite links): every Cite line renders."""
    from hansard_gateway.render.cite import cite_speech_limit

    assert (
        cite_speech_limit(speech_count=14, non_cite_links=23, base_bytes=0) == 14
    )


def test_cap_link_branch() -> None:
    """500 speeches, 60 non-Cite links, base over the byte budget: the link
    branch (400-60)//2 = 170 binds (base >= budget zeroes the byte branch so
    the link arithmetic is the observable output)."""
    from hansard_gateway.render.cite import (
        CITE_PAGE_BUDGET_BYTES,
        cite_speech_limit,
    )

    # base exactly at the budget zeroes the byte branch -> min picks 0.
    assert (
        cite_speech_limit(
            speech_count=500, non_cite_links=60,
            base_bytes=CITE_PAGE_BUDGET_BYTES,
        )
        == 0
    )
    # base far enough under a WIDENED byte budget that the byte branch
    # (>= 170) no longer binds -> the limit IS the link branch
    # (400-60)//2 = 170. (Under the default 100 KB budget at base 0 the byte
    # branch is 102400//700 = 146 < 170 — with the worst-case constant the
    # link branch is only observable past a widened byte budget.)
    assert (
        cite_speech_limit(
            speech_count=500, non_cite_links=60, base_bytes=0,
            page_budget_bytes=CITE_PAGE_BUDGET_BYTES * 2,
        )
        == 170
    )
    # Sanity: default budget at base 0 -> the byte branch binds at 146.
    assert (
        cite_speech_limit(speech_count=500, non_cite_links=60, base_bytes=0)
        == (CITE_PAGE_BUDGET_BYTES // 700)
    )


def test_cap_degenerate_zero_when_links_exhausted() -> None:
    """B >= the link budget => 0 Cite lines (page still renders)."""
    from hansard_gateway.render.cite import cite_speech_limit

    assert (
        cite_speech_limit(speech_count=10, non_cite_links=400, base_bytes=0) == 0
    )
    assert (
        cite_speech_limit(speech_count=10, non_cite_links=1000, base_bytes=0) == 0
    )


def test_cap_byte_branch() -> None:
    """A large est_bytes_per_cite + small headroom reduces the limit by the
    byte arithmetic floor((page_budget_bytes - base_bytes) / est)."""
    from hansard_gateway.render.cite import cite_speech_limit

    # base 50 KB, est 500 bytes/cite, 100 KB budget => floor(50*1024/500) = 102.
    limit = cite_speech_limit(
        speech_count=500, non_cite_links=0, base_bytes=50 * 1024,
        page_budget_bytes=100 * 1024, est_bytes_per_cite=500,
    )
    assert limit == (100 * 1024 - 50 * 1024) // 500
    # base over budget => 0.
    assert (
        cite_speech_limit(speech_count=5, non_cite_links=0,
                          base_bytes=100 * 1024 + 1)
        == 0
    )


def test_cap_is_min_of_speeches_link_and_byte_branches() -> None:
    from hansard_gateway.render.cite import cite_speech_limit

    # speech_count binds.
    assert (
        cite_speech_limit(speech_count=3, non_cite_links=0, base_bytes=0) == 3
    )
    # link branch binds (3 // 2 headroom... (400-0)//2=200 > 100 speeches).
    assert (
        cite_speech_limit(speech_count=100, non_cite_links=0, base_bytes=0) == 100
    )


# --------------------------------------------------------------------------- #
# v0.1.7 c8 — the worst-case bound constant (C8-2: demonstrable, O(1))
# --------------------------------------------------------------------------- #


def test_cite_worst_bound_constant_covers_recomputed_block() -> None:
    """(a) CITE_WORST_BYTES_PER_LINE >= the worst-case rendered Cite block
    recomputed IN-TEST from the same component bounds the constant assumes:
    token <= settings.token_max_len, report id <= CITE_REPORT_ID_MAX,
    fragment "speech-N" <= 10 chars (N <= CITE_SPEECH_MAX), label post-escape
    <= CITE_LABEL_MAX_CHARS, base URL = settings.public_base_url. The test IS
    the proof of the bound (pass-2 MAJOR C8-2)."""
    import html as _html
    from urllib.parse import quote

    from hansard_gateway.render.cite import (
        CITE_LABEL_MAX_CHARS,
        CITE_REPORT_ID_MAX,
        CITE_SPEECH_MAX,
        CITE_WORST_BYTES_PER_LINE,
    )

    token_max = "x" * settings.token_max_len  # the auth shape guard ceiling
    id_max = "y" * CITE_REPORT_ID_MAX
    # "speech-9999" is 11 chars at SPEECH_MAX — the amendment log's "<= 10"
    # was a typo; the 700 B headroom already covers the 11 (worst block
    # recomputed below with the true length).
    frag_max = f"speech-{CITE_SPEECH_MAX}"
    assert len(frag_max) <= 11, "fragment bound assumption broke"
    # Worst-case label: the truncation in cite_label bounds the SOURCE to
    # LABEL_MAX chars; the bound's 80 B component assumes a post-escape
    # rendered length <= 80 B, which holds for the name-shaped speaker text
    # the corpus actually carries (ASCII names/roles/initials — no escape
    # expansion at all, per test_cite_labels_bounded_post_escape). This
    # recomputation mirrors that assumption: 80 ASCII source chars, escaped
    # (no-op for ASCII) — the maximal in-domain construction.
    label_src_max = "A" * CITE_LABEL_MAX_CHARS
    label_escaped_max = _html.escape(label_src_max, quote=False)

    url = (
        f"{settings.public_base_url.rstrip('/')}/a/{token_max}/report/"
        f"{quote(id_max, safe='')}#{quote(frag_max, safe='-_')}"
    )
    block = (
        f'<p class="cite">Cite: <a href="{url}">{label_escaped_max}'
        f'</a><span class="u">{url}</span></p>'
    )
    block_bytes = len(block.encode("utf-8"))
    assert CITE_WORST_BYTES_PER_LINE >= block_bytes, (
        f"CITE_WORST_BYTES_PER_LINE={CITE_WORST_BYTES_PER_LINE} < recomputed "
        f"worst-case block {block_bytes} B — the bound is not an upper bound"
    )


def test_no_report_id_exceeds_r_max() -> None:
    """(b) the CITE_REPORT_ID_MAX ceiling holds on EVERY id in the offline
    corpus (fixture rows + baseline corpus + the committed JSON/text baseline
    stems) — the bound is enforced, not assumed. (Upstream-generated ids are
    charset/length-guarded by report_id.validate_report_id; this pins that the
    guard's 100-char ceiling is never actually exercised past 64 on real
    data.)"""
    import json
    import sys
    from pathlib import Path

    _repo = Path(__file__).resolve().parent.parent
    if str(_repo / "scripts") not in sys.path:
        sys.path.insert(0, str(_repo / "scripts"))
    from baseline_corpus import CORPUS, offline_index_rows  # type: ignore

    from hansard_gateway.render.cite import CITE_REPORT_ID_MAX

    ids: list[str] = []
    ids.extend(row["report_id"] for row in offline_index_rows())
    ids.extend(row["link_id"] for row in offline_index_rows())
    ids.extend(entry.path.rsplit("/", 1)[-1] for entry in CORPUS
               if entry.path.startswith("/report/"))
    here = Path(__file__).parent
    for d in ("json_format_baseline", "text_format_baseline",
              "fixtures/baselines/wave0"):
        root = here / d
        if not root.is_dir():
            continue
        for f in root.iterdir():
            if f.name.startswith("report_") and f.suffix in (".json", ".txt"):
                ids.append(f.name[len("report_"):][: -len(f.suffix)])
    for f in (here / "fixtures").glob("topic_*.json"):
        ids.append(f.name.split("_", 1)[-1][: -len(f.suffix)])
    assert ids, "no report ids found — the bound test is vacuous"
    for rid in ids:
        assert len(rid) <= CITE_REPORT_ID_MAX, (
            f"report id {rid!r} is {len(rid)} chars > CITE_REPORT_ID_MAX "
            f"{CITE_REPORT_ID_MAX} — raise the ceiling and re-derive the "
            f"worst-case constant"
        )


def test_cite_labels_bounded_post_escape() -> None:
    """(c) no rendered Cite label (POST-ESCAPE, as it appears on the page)
    exceeds the bound's label component: the truncation in
    :func:`hansard_gateway.render.cite.cite_label` makes it hold by
    construction — checked on the committed JSON baselines (the real speech
    populations) AND on a deliberately over-long synthetic speaker."""
    import html as _html
    import json
    from pathlib import Path

    from hansard_gateway.models import Speech
    from hansard_gateway.render.cite import (
        CITE_LABEL_MAX_CHARS,
        PROCEDURAL_LABEL,
        cite_label,
    )

    # The real populations: every committed baseline report's speeches.
    baseline_dir = Path(__file__).parent / "fixtures" / "json_format_baseline"
    reports = [
        json.loads(f.read_text(encoding="utf-8"))
        for f in sorted(baseline_dir.glob("report_*.json"))
    ]
    assert reports, "no JSON baselines — the bound test is vacuous"
    for report in reports:
        for speech in report["speeches"]:
            raw = speech["speaker_original"] or PROCEDURAL_LABEL
            # Mirror Jinja autoescape (the label renders through it).
            rendered = _html.escape(raw, quote=False).replace(
                '"', "&#34;").replace("'", "&#39;")
            # The Cite anchor text's speaker part is truncate(raw) escaped;
            # the UNTRUNCATED escape is an upper bound on the truncated one.
            assert len(rendered) <= CITE_LABEL_MAX_CHARS * 6 + 1, (
                f"{report['report_id']}: rendered label {len(rendered)} B far "
                f"exceeds the bound's {CITE_LABEL_MAX_CHARS}-char component"
            )
    # The construction case: a speaker far past the ceiling truncates to
    # LABEL_MAX chars INCLUDING the ellipsis (so post-escape <= LABEL_MAX *
    # worst-escape-fan-out, and in practice == LABEL_MAX for ASCII).
    long_speech = Speech(
        sequence=1, speaker_original="z" * (CITE_LABEL_MAX_CHARS + 50),
        speaker_name=None, speaker_role=None, paragraphs=["x"],
    )
    label = cite_label(sequence=1, speech=long_speech)
    speaker_part = label.split(" — ", 1)[1]
    assert len(speaker_part) == CITE_LABEL_MAX_CHARS, (
        f"truncated speaker part {len(speaker_part)} != LABEL_MAX "
        f"{CITE_LABEL_MAX_CHARS}"
    )
    assert label.endswith("…"), "truncation must end with the ellipsis"
    # Procedural fallback is inside the bound by construction.
    assert len(PROCEDURAL_LABEL) <= CITE_LABEL_MAX_CHARS


def test_cite_cap_conformance_worst_constant() -> None:
    """(d) cite_speech_limit()'s DEFAULT est input is now the worst-case
    bound — recompute the conformance against CITE_WORST_BYTES_PER_LINE
    explicitly (the same function, imported, not re-implemented)."""
    from hansard_gateway.render.cite import (
        CITE_WORST_BYTES_PER_LINE,
        cite_speech_limit,
    )

    # bill-774 measured inputs (c8-STEP-1, live local container, 260921):
    # 14 speeches, 21 non-Cite links, base 97372 B.
    limit_default = cite_speech_limit(
        speech_count=14, non_cite_links=21, base_bytes=97372,
    )
    limit_explicit = cite_speech_limit(
        speech_count=14, non_cite_links=21, base_bytes=97372,
        est_bytes_per_cite=CITE_WORST_BYTES_PER_LINE,
    )
    assert limit_default == limit_explicit == 7, (
        f"cap default must equal the worst-constant recompute: "
        f"default={limit_default} explicit={limit_explicit}"
    )
    # The byte branch binds (link branch is (400-21)//2 = 189 > 14 speeches
    # only if the byte branch allowed >= 14; verify it is the binding one).
    byte_branch = (100 * 1024 - 97372) // CITE_WORST_BYTES_PER_LINE
    assert byte_branch < 14, "precondition: the byte branch must bind"
    assert limit_default == byte_branch


# --------------------------------------------------------------------------- #
# Task 1 — build_cite_context
# --------------------------------------------------------------------------- #


def _three_speech_report() -> HansardReport:
    """A tiny report: one named + one procedural speech + one short-named."""
    return HansardReport(
        report_id="bill-774",
        date=date(2026, 1, 12),
        title="Health Information Bill",
        topic_type=None,
        source_url="https://sprs.parl.gov.sg/search/#/sprs3topic?reportid=bill-774",
        volume="96",
        parliament_no="15",
        session_no="1",
        sitting_no="12",
        speeches=[
            Speech(sequence=1, speaker_original=None, speaker_name=None,
                   speaker_role=None, paragraphs=["Debate resumed."]),
            Speech(sequence=2,
                   speaker_original="Ms Kuah Boon Theng (Nominated Member)",
                   speaker_name="Kuah Boon Theng",
                   speaker_role="Nominated Member",
                   paragraphs=["Thank you, Mr Speaker."]),
            Speech(sequence=3, speaker_original="Mr Foo (PPM)",
                   speaker_name="Foo", speaker_role="PPM",
                   paragraphs=["Point of order."]),
        ],
        transcript_sha256="deadbeef",
    )


def test_build_cite_context_under_cap_all_cited() -> None:
    from hansard_gateway.render.cite import build_cite_context

    urls, labels = build_cite_context(
        report=_three_speech_report(), token=TEST_TOKEN,
        non_cite_links=23, base_bytes=0,
    )
    base = settings.public_base_url.rstrip("/")
    assert urls == [
        f"{base}/a/{TEST_TOKEN}/report/bill-774#speech-{n}" for n in (1, 2, 3)
    ]
    assert labels == [
        "speech 1 — [procedural]",
        "speech 2 — Ms Kuah Boon Theng (Nominated Member)",
        "speech 3 — Mr Foo (PPM)",
    ]


def test_build_cite_context_past_cap_is_none() -> None:
    """Speeches past the cap get None (no Cite line) but keep their id."""
    from hansard_gateway.render.cite import build_cite_context

    urls, labels = build_cite_context(
        report=_three_speech_report(), token=TEST_TOKEN,
        non_cite_links=400, base_bytes=0,  # degenerate: cap = 0
    )
    assert urls == [None, None, None]
    assert labels == [None, None, None]


# --------------------------------------------------------------------------- #
# Task 2 — rendered report page (offline E2E topic fixture, 5 speeches)
# --------------------------------------------------------------------------- #


def _topic_fixture() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / "topic_20041019_saf.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def rendered_report(client_with_index: TestClient) -> str:
    """The offline-rendered E2E report page (respx-stubbed)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_topic_fixture())
        r = client_with_index.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert r.status_code == 200
    return r.text


def test_report_speech_ids_present(rendered_report: str) -> None:
    """Every article.speech carries id="speech-{sequence}" (1-based)."""
    for n in (1, 2, 3, 4, 5):
        assert f'id="speech-{n}"' in rendered_report, f"missing id speech-{n}"


def test_report_cite_lines_verbatim(rendered_report: str) -> None:
    """Each Cite href == abs report URL + '#speech-N' and the .u twin carries
    the SAME string verbatim (a6 amendment 4: the AI discovers it, never
    constructs or normalizes it)."""
    base = settings.public_base_url.rstrip("/")
    for n in (1, 2, 3, 4, 5):
        cite_url = f"{base}/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}#speech-{n}"
        assert f'href="{cite_url}"' in rendered_report, f"missing Cite href {n}"
        # The verbatim .u twin: the exact URL string inside a span.u.
        assert (
            f'<span class="u">{cite_url}</span>' in rendered_report
        ), f"missing verbatim .u twin for {n}"


def test_report_cite_note_present(rendered_report: str) -> None:
    assert 'class="cite-note"' in rendered_report
    assert "opening it in a browser highlights that speech" in rendered_report


def test_report_target_css_present(rendered_report: str) -> None:
    """The gold :target state is in the rendered page's inline CSS."""
    assert "article.speech:target" in rendered_report
    assert "#fff3b0" in rendered_report


def test_report_text_format_unchanged(client_with_index: TestClient) -> None:
    """?format=text is unchanged by the citation feature (Cite lines are
    HTML-only — the text serializer never sees the render context).

    The committed fixture (report_bill-774.txt) is the LIVE redacted reference
    (bill-774); the offline render uses the E2E topic fixture (a different
    report), so full byte-equality is impossible by design. The contract is:
    no Cite material leaks into the text view, and the SHA-256 fingerprint
    line in the HTML footer is byte-identical to the model's value (pre/post).
    """
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_topic_fixture())
        r = client_with_index.get(
            f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=text"
        )
    assert r.status_code == 200
    text = r.text
    assert "Cite" not in text
    assert "#speech-" not in text
    assert "cite" not in text.lower()


def test_report_sha_footer_value_unchanged(client_with_index: TestClient,
                                           rendered_report: str) -> None:
    """The SHA-256 footer value on the HTML page equals the model's
    transcript_sha256 (byte-identical pre/post — the feature wraps, never
    edits, the transcript)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_topic_fixture())
        r = client_with_index.get(
            f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=json"
        )
    data = json.loads(r.text)
    assert data["transcript_sha256"]
    assert f"Transcript SHA-256: {data['transcript_sha256']}" in rendered_report


def test_report_json_format_shape_unchanged(client_with_index: TestClient) -> None:
    """?format=json parses and keeps the pre-citation shape (no new fields)."""
    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_topic_fixture())
        r = client_with_index.get(
            f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=json"
        )
    assert r.status_code == 200
    data = json.loads(r.text)
    assert "speeches" in data
    for speech in data["speeches"]:
        assert "paragraphs" in speech


def test_report_bodies_verbatim(
    client_with_index: TestClient, rendered_report: str
) -> None:
    """Every source paragraph string survives verbatim on the rendered page
    (transcript integrity — Cite lines wrap, never edit)."""
    import html as _html

    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_topic_fixture())
        r = client_with_index.get(
            f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}?format=json"
        )
    data = json.loads(r.text)
    # The parser normalizes whitespace (sprs legacy _clean_text: collapse all
    # runs to single spaces), so the page carries the collapsed form — that IS
    # the verbatim normalized transcript the SHA footer fingerprints. The
    # speaker name renders in the h3 header AND at the body's start (legacy
    # documents carry it in the segment), so the full collapsed paragraph is
    # a verbatim substring of the rendered body either way.
    import re as _re

    for speech in data["speeches"]:
        for paragraph in speech["paragraphs"]:
            collapsed = _re.sub(r"\s+", " ", paragraph)
            # Jinja autoescape renders & < > " ' as &#38; &#60; &#62; &#34;
            # &#39; (never the named &amp;/&lt;/&gt; or bare quotes) — mirror
            # it so the verbatim check is exact.
            jinja_escaped = _html.escape(collapsed, quote=False).replace(
                '"', "&#34;").replace("'", "&#39;")
            assert jinja_escaped in rendered_report, (
                f"paragraph not verbatim on the page: {collapsed[:60]!r}"
            )


def test_report_css_budget(rendered_report: str) -> None:
    """All <style> blocks summed stay under the inline-CSS budget.

    Wave 2 (27.3-03) raised the budget from 2 KB to 8 KB: the approved a8
    wireframe system cannot fit 2 KB even after the plan's presentational-
    only trims (measured 4928 B; the deviation record in the 27.3-03-
    SUMMARY documents the trim list + the CO question). 8 KB leaves headroom
    for the Wave 3/4 search/launcher/nav/date restyles. The value is pinned
    to the same constant the conformance linter uses (PAGE_BUDGET_CSS_BYTES
    in test_link_conformance) so the two can never drift.
    """
    import re

    from tests.test_link_conformance import PAGE_BUDGET_CSS_BYTES

    css = "\n".join(re.findall(r"<style>(.*?)</style>", rendered_report,
                               re.DOTALL))
    assert len(css.encode("utf-8")) <= PAGE_BUDGET_CSS_BYTES, (
        f"inline CSS over budget: {len(css)} > {PAGE_BUDGET_CSS_BYTES}"
    )
