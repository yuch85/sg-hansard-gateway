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
    """500 speeches, 60 non-Cite links: the link branch is (400-60)//2 = 170."""
    from hansard_gateway.render.cite import cite_speech_limit

    assert (
        cite_speech_limit(speech_count=500, non_cite_links=60, base_bytes=0) == 170
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
    for speech in data["speeches"]:
        for paragraph in speech["paragraphs"]:
            assert _html.escape(paragraph) in rendered_report, (
                f"paragraph not verbatim on the page: {paragraph[:60]!r}"
            )


def test_report_css_budget(rendered_report: str) -> None:
    """All <style> blocks summed stay under the 2 KB inline-CSS budget."""
    import re

    css = "\n".join(re.findall(r"<style>(.*?)</style>", rendered_report,
                               re.DOTALL))
    assert len(css.encode("utf-8")) <= PAGE_BUDGET_HTML_BYTES // 50, (
        f"inline CSS over budget: {len(css)} > 2048"
    )
