"""Offline sprs2 (pre-2012) parser tests — fixture-driven, no network.

Loads the 2004 SAF topic payload (``topic_20041019_saf.json``), runs the sprs2
parser, and asserts verbatim integrity: meta extraction, MP_NAME-comment
speakers (empty = procedural), kept ``Column:`` marks, and a stable SHA-256
transcript fingerprint (D-16, spec §14).
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import hansard_gateway.sprs.payload as payload_mod
from hansard_gateway.models import HansardReport
from hansard_gateway.report_id import normalize_for_topic
from hansard_gateway.sprs.payload import TopicPayload, from_result_html, parse_topic

#: The E2E target report ID (spec §30-A).
E2E_REPORT_ID = "037_20041019_S0004_T0023"

#: A 64-char lowercase hex SHA-256 digest.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: The two MP_NAME-comment speakers (verbatim, from the fixture meta).
SPEAKER_ONG = "Dr Ong Chit Chung (Jurong)"
SPEAKER_FOO = "The Minister of State for Defence (Mr Cedric Foo Chee Keng)"


def _load_payload(fixtures_dir: Path) -> dict[str, object]:
    """Load the sprs2 topic fixture (flat object: htmlContent at the root)."""
    path = fixtures_dir / "topic_20041019_saf.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _report(fixtures_dir: Path) -> HansardReport:
    """Parse the sprs2 fixture into a HansardReport (offline)."""
    raw = _load_payload(fixtures_dir)
    result_html = {k: raw[k] for k in raw}  # sprs2 is a flat object
    normalized_id = normalize_for_topic(report_id=E2E_REPORT_ID)
    payload = from_result_html(
        result_html,
        source_url=payload_mod.topic_source_url(
            report_id=normalized_id, sitting_date_iso="2004-10-19"
        ),
    )
    return parse_topic(payload, report_id=E2E_REPORT_ID)


def test_title_contains_saf(fixtures_dir: Path) -> None:
    """The official title is extracted and contains 'Singapore Armed Forces'.

    The upstream meta ``Title`` is ALL-CAPS; the spec's sentence-case form is a
    presentation choice, so the assertion is case-insensitive.
    """
    report = _report(fixtures_dir)
    assert "Singapore Armed Forces".casefold() in report.title.casefold()


def test_date_is_2004_10_19(fixtures_dir: Path) -> None:
    """Sit_Date (YYYY-MM-DD in sprs2) parses to 2004-10-19."""
    report = _report(fixtures_dir)
    assert report.date == date(2004, 10, 19)


def test_speakers_present(fixtures_dir: Path) -> None:
    """Both MP_NAME-comment speakers appear verbatim as speakers."""
    report = _report(fixtures_dir)
    speakers = [s.speaker_original for s in report.speeches if s.speaker_original]
    assert SPEAKER_ONG in speakers
    assert SPEAKER_FOO in speakers


def test_procedural_speaker_none(fixtures_dir: Path) -> None:
    """An empty <!--MP_NAME:--> yields at least one procedural (None) speech."""
    report = _report(fixtures_dir)
    assert any(s.speaker_original is None for s in report.speeches)


def test_column_mark_preserved(fixtures_dir: Path) -> None:
    """A 'Column:' mark is preserved verbatim in the transcript text."""
    report = _report(fixtures_dir)
    transcript = " ".join(
        p for s in report.speeches for p in s.paragraphs
    )
    assert "Column:" in transcript


def test_paragraph_counts_derived_from_fixture(fixtures_dir: Path) -> None:
    """F3: per-speech paragraph counts match the fixture's real <p> structure.

    The expected count is computed PROGRAMMATICALLY from the committed fixture
    (count the actual <p> blocks per speaker segment using the same cleaning
    the parser applies) — never hard-coded.
    """
    from hansard_gateway.sprs.legacy import (
        _clean_text,
        _paragraph_blocks,
        _split_segments,
    )

    raw = _load_payload(fixtures_dir)
    html = raw["htmlContent"]  # type: ignore[index]
    segments = _split_segments(html)

    report = _report(fixtures_dir)
    substantive = [
        frag for _, frag in segments if len(_clean_text(frag)) >= 3
    ]
    assert len(report.speeches) == len(substantive), (
        f"speech count {len(report.speeches)} != "
        f"substantive segments {len(substantive)}"
    )

    for speech, (speaker, fragment) in zip(report.speeches, segments):
        if len(_clean_text(fragment)) < 3:
            continue
        expected = _paragraph_blocks(fragment)
        assert len(speech.paragraphs) == len(expected), (
            f"speaker {speaker!r}: got {len(speech.paragraphs)} paragraphs, "
            f"expected {len(expected)} from fixture <p> blocks"
        )


def test_exact_transformation_preservation(fixtures_dir: Path) -> None:
    """F3 (copilot pass-1 Major 6): per-segment exact-transformation proof.

    For each speaker segment: old_text = _clean_text(segment) and
    new_paragraphs = [clean(p) for p in <p> blocks if nonempty], then
    old_text == " ".join(new_paragraphs). This proves ONLY paragraph
    boundaries were restored — no body character edited.
    """
    from hansard_gateway.sprs.legacy import (
        _clean_text,
        _paragraph_blocks,
        _split_segments,
    )

    raw = _load_payload(fixtures_dir)
    html = raw["htmlContent"]  # type: ignore[index]
    segments = _split_segments(html)

    for speaker, fragment in segments:
        old_text = _clean_text(fragment)
        if len(old_text) < 3:
            continue
        new_paragraphs = _paragraph_blocks(fragment)
        assert old_text == " ".join(new_paragraphs), (
            f"speaker {speaker!r}: old_text != ' '.join(new_paragraphs) — "
            f"a body character was edited, not just boundaries restored"
        )


def test_transcript_sha256_stable(fixtures_dir: Path) -> None:
    """The SHA-256 fingerprint is 64 hex chars and stable across re-parses."""
    first = _report(fixtures_dir)
    second = _report(fixtures_dir)
    assert _SHA256_RE.fullmatch(first.transcript_sha256) is not None
    assert first.transcript_sha256 == second.transcript_sha256


def test_metadata_fields(fixtures_dir: Path) -> None:
    """Parl/Session/Volume/Sitting metadata is extracted from the payload."""
    report = _report(fixtures_dir)
    assert report.parliament_no == "10"
    assert report.session_no == "1"
    assert report.volume == "78"
    assert report.sitting_no == "6"
    assert report.report_id == E2E_REPORT_ID


def test_source_url_no_token(fixtures_dir: Path) -> None:
    """The provenance URL is the official SPRS SECTION record, no token.

    Mission 008 (verified in a real browser 2026-09-20): spec-style ids
    land on the SPA's #/topic route with the htmlFileName — the section
    page, not the full sitting.
    """
    report = _report(fixtures_dir)
    assert report.source_url == (
        "https://sprs.parl.gov.sg/search/#/topic"
        "?reportid=037_20041019_S0004_T0023"
    )
    assert "hg_" not in report.source_url
