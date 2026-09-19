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
    """The provenance URL is the official SPRS sitting record, no token.

    sprs2 (pre-2013) sits land on the SPA's #/report route with the
    sitting date (the /hansard/<id> scheme 404s — SPRS is an SPA with no
    public per-report URL; verified live 2026-09-19).
    """
    report = _report(fixtures_dir)
    assert report.source_url == (
        "https://sprs.parl.gov.sg/search/#/report?sittingdate=19-10-2004"
    )
    assert "hg_" not in report.source_url
