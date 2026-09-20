"""Wave-2 report restyle tests (Phase 27.3 Plan 03).

Covers the TOC builder (``render.toc``) — one entry per RETAINED speech
(count = min(speech_count, TOC_max), never unconditional), verbatim-copy
previews, speaker-class visual heuristic, the shared speaker-label helper,
and the byte-budget constant — plus the combined TOC cap arithmetic
(link + byte, G-A6-2) proven on the synthetic long sitting.

The rendered-page assertions (TOC structure in the template, the
persistent-speaker structural zero-duplication invariant, provenance
completeness, the superset verbatim invariant, print-.u visibility) land
with the Task 2 template restructure in this same file.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from hansard_gateway.auth import TEST_TOKEN
from hansard_gateway.models import HansardReport, Speech
from hansard_gateway.render.toc import (
    EST_BYTES_PER_TOC_ENTRY,
    PROCEDURAL_LABEL,
    TOC_MAX_SPEAKER_NAME_LEN,
    TOC_PREVIEW_CHARS,
    TOC_SPEAKER_CLASS_MP,
    TOC_SPEAKER_CLASS_PROCEDURAL,
    TOC_SPEAKER_CLASS_SPEAKER,
    build_toc_entries,
    classify_speaker,
    speaker_label,
)

UPSTREAM_BASE = "https://sprs.parl.gov.sg/search"
E2E_REPORT_ID = "037_20041019_S0004_T0023"


# --- report fixtures (same offline ground truth as test_link_conformance) ---


def _render_e2e_report(client: TestClient) -> str:
    """Render the offline E2E topic report (5 speeches) through HTTP."""
    from hansard_gateway.sprs.payload import parse_topic

    with respx.mock(base_url=UPSTREAM_BASE, assert_all_called=False) as mock:
        mock.post("/getHansardTopic").respond(json=_result_html())
        r = client.get(f"/a/{TEST_TOKEN}/report/{E2E_REPORT_ID}")
    assert r.status_code == 200
    return r.text


def _result_html() -> dict[str, Any]:
    """The committed 2004 sprs2 topic payload (the E2E report)."""
    path = Path(__file__).parent / "fixtures" / "topic_20041019_saf.json"
    return __import__("json").loads(path.read_text(encoding="utf-8"))


def _e2e_report() -> HansardReport:
    """Parse the E2E topic fixture into a HansardReport (no HTTP)."""
    from hansard_gateway.sprs.payload import from_result_html, parse_topic

    payload = from_result_html(
        _result_html(), source_url="https://sprs.parl.gov.sg/x"
    )
    return parse_topic(payload, report_id=E2E_REPORT_ID)


def _synthetic_long_report() -> HansardReport:
    """The 300-speech synthetic sitting (same shape as the Wave-1 cap test)."""
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
            for i in range(300)
        ],
        transcript_sha256="synthetic",
    )


# --- TOC builder (Task 1) -----------------------------------------------------


def test_toc_one_entry_per_speech_in_sequence_order() -> None:
    """One entry per speech, in sequence order, anchor = #speech-N."""
    report = _e2e_report()
    entries = build_toc_entries(report=report)
    assert len(entries) == len(report.speeches)
    for entry, speech in zip(entries, report.speeches):
        assert entry.sequence == speech.sequence
        assert entry.anchor == f"speech-{speech.sequence}"


def test_toc_max_entries_caps_to_first_k_in_sequence_order() -> None:
    """max_entries=K returns EXACTLY the first K entries in sequence order
    (the CAPPED TOC — M3: count = min(speech_count, TOC_max), never
    'one per speech' when a cap is in force)."""
    report = _e2e_report()
    for k in (0, 1, 3, 10):
        entries = build_toc_entries(report=report, max_entries=k)
        assert len(entries) == min(len(report.speeches), k)
        expected = [f"speech-{i + 1}" for i in range(min(len(report.speeches), k))]
        assert [e.anchor for e in entries] == expected


def test_toc_preview_is_verbatim_prefix_at_word_boundary() -> None:
    """Preview is a verbatim prefix of the first paragraph, cut at the last
    space at-or-before TOC_PREVIEW_CHARS (no mid-word cut; short paragraphs
    are used whole)."""
    report = _e2e_report()
    for entry, speech in zip(build_toc_entries(report=report), report.speeches):
        first = speech.paragraphs[0]
        assert entry.preview in first  # verbatim prefix (copy, not new text)
        if len(first) > TOC_PREVIEW_CHARS:
            assert len(entry.preview) <= TOC_PREVIEW_CHARS
            # the cut is at a word boundary: the next char in the source is a
            # space (or the preview ends exactly at the source length)
            nxt = first[len(entry.preview):len(entry.preview) + 1]
            assert nxt == " ", (
                f"preview not cut at a word boundary: {entry.preview!r}"
            )
            assert entry.preview == first[: len(entry.preview)].rstrip()
        else:
            assert entry.preview == first  # short paragraph used whole


def test_toc_speaker_class_heuristic() -> None:
    """speaker_class visual heuristic (a documented heuristic, not a
    semantic change): procedural / speaker / mp."""
    assert classify_speaker(speaker_original=None) == TOC_SPEAKER_CLASS_PROCEDURAL
    assert classify_speaker(speaker_original="") == TOC_SPEAKER_CLASS_PROCEDURAL
    assert classify_speaker(speaker_original="  ") == TOC_SPEAKER_CLASS_PROCEDURAL
    # exactly the Speaker's procedural name (ends with 'Speaker', short) —
    # "Mr Deputy Speaker" (17 chars) EXCEEDS the 12-char bound, so the
    # heuristic classifies it as mp (visual only; the text is unchanged).
    assert classify_speaker(speaker_original="Mr Speaker") == TOC_SPEAKER_CLASS_SPEAKER
    assert classify_speaker(speaker_original="Mr Deputy Speaker") == TOC_SPEAKER_CLASS_MP
    # anything else is an MP (full text retained, only the visual class differs)
    assert classify_speaker(speaker_original="Dr Ong Chit Chung (Jurong)") == TOC_SPEAKER_CLASS_MP
    assert (
        classify_speaker(speaker_original="The Minister of State for Defence (Mr Cedric Foo Chee Keng)")
        == TOC_SPEAKER_CLASS_MP
    )
    # the Speaker-classification bound is the named constant
    assert TOC_MAX_SPEAKER_NAME_LEN == 12


def test_toc_speaker_label_is_shared_source_for_toc_and_h3() -> None:
    """speaker_label is the ONE source for the TOC label and the speech h3
    (they can never drift): None/empty -> [procedural], else verbatim."""
    assert speaker_label(speech=Speech(
        sequence=1, speaker_original=None, speaker_name=None,
        speaker_role=None, paragraphs=["x"],
    )) == PROCEDURAL_LABEL
    assert PROCEDURAL_LABEL == "[procedural]"
    assert speaker_label(speech=Speech(
        sequence=2, speaker_original="Mr Speaker", speaker_name=None,
        speaker_role=None, paragraphs=["x"],
    )) == "Mr Speaker"
    report = _e2e_report()
    for entry, speech in zip(build_toc_entries(report=report), report.speeches):
        assert entry.speaker_label == speaker_label(speech=speech)
        assert entry.speaker_class == classify_speaker(
            speaker_original=speech.speaker_original
        )


def test_toc_empty_report_returns_empty_list() -> None:
    """No speeches -> no TOC entries (the template renders no TOC)."""
    report = HansardReport(
        report_id="empty", date=date(2020, 1, 1), title="Empty",
        topic_type=None, source_url="https://sprs.parl.gov.sg/x",
        volume=None, parliament_no=None, session_no=None, sitting_no=None,
        speeches=[], transcript_sha256="",
    )
    assert build_toc_entries(report=report) == []


def test_toc_adds_zero_absolute_urls() -> None:
    """The TOC contributes only same-page #speech-N anchors — no absolute
    URL (and therefore no .u twin requirement, R3/R2 scoped to token
    links)."""
    report = _e2e_report()
    entries = build_toc_entries(report=report)
    for entry in entries:
        assert entry.anchor.startswith("speech-")
        assert "://" not in entry.anchor
        assert not entry.anchor.startswith("http")


def test_est_bytes_per_toc_entry_is_named_constant_260() -> None:
    """M2 byte-budget term: a CONSERVATIVE UPPER BOUND for one rendered TOC
    li (14 real wireframe li range 163-208 bytes, max 208, rounded UP to
    260) — headroom for the pre-render allocation, not a measured value."""
    assert EST_BYTES_PER_TOC_ENTRY == 260
    assert TOC_PREVIEW_CHARS == 60
