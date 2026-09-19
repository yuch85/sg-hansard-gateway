"""Offline sprs3 (post-2012) parser tests — fixture-driven, no network.

Loads the Workplace Fairness Bill topic payload (``topic_20250108_bill742.json``),
runs the sprs3 parser, and asserts verbatim integrity: ``<strong>`` speaker
extraction, same-speaker merging, preserved ``(proc text)`` markers and ``<h6>``
clock marks, unpadded-date → ISO parsing (Pitfall 6), and a stable SHA-256
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
from hansard_gateway.sprs.payload import from_result_html, parse_topic

#: The live report ID (post-2012 era; trailing # stripped by normalize).
BILL_REPORT_ID = "bill-742"

#: A 64-char lowercase hex SHA-256 digest.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Two <strong> speakers (verbatim, from the fixture).
SPEAKER_SPEAKER = "Mr Speaker"
SPEAKER_SAKTIANDI = "Mr Saktiandi Supaat (Bishan-Toa Payoh)"

#: Verbatim markers that must survive parsing (Pitfall 8).
PROC_MARKER = "(proc text)"
CLOCK_MARKER = "2.45 pm"


def _load_result_html(fixtures_dir: Path) -> dict[str, object]:
    """Load the sprs3 topic fixture's resultHTML object."""
    path = fixtures_dir / "topic_20250108_bill742.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw["resultHTML"]


def _report(fixtures_dir: Path) -> HansardReport:
    """Parse the sprs3 fixture into a HansardReport (offline)."""
    result_html = _load_result_html(fixtures_dir)
    normalized_id = normalize_for_topic(report_id=BILL_REPORT_ID)
    payload = from_result_html(
        result_html,
        source_url=payload_mod.topic_source_url(
            report_id=normalized_id, sitting_date_iso="2025-01-08"
        ),
    )
    return parse_topic(payload, report_id=BILL_REPORT_ID)


def test_speakers_from_strong(fixtures_dir: Path) -> None:
    """Speakers come from <strong> prefixes (Mr Speaker, Mr Saktiandi)."""
    report = _report(fixtures_dir)
    speakers = [s.speaker_original for s in report.speeches if s.speaker_original]
    assert SPEAKER_SPEAKER in speakers
    assert SPEAKER_SAKTIANDI in speakers


def test_consecutive_same_speaker_merged(fixtures_dir: Path) -> None:
    """Consecutive same-speaker paragraphs are merged into one Speech run."""
    report = _report(fixtures_dir)
    # The fixture has many carry-forward paragraphs, so some run must hold
    # more than one paragraph (proves merging happened).
    assert any(len(s.paragraphs) > 1 for s in report.speeches)
    # And a specific known multi-paragraph speaker: Mr Saktiandi's opening
    # speech spans several carry-forward paragraphs.
    saktiandi = [
        s for s in report.speeches if s.speaker_original == SPEAKER_SAKTIANDI
    ]
    assert saktiandi and any(len(s.paragraphs) > 1 for s in saktiandi)


def test_proc_text_preserved(fixtures_dir: Path) -> None:
    """A '(proc text)' marker is preserved verbatim in the transcript."""
    report = _report(fixtures_dir)
    transcript = " ".join(p for s in report.speeches for p in s.paragraphs)
    assert PROC_MARKER in transcript


def test_clock_mark_preserved(fixtures_dir: Path) -> None:
    """An <h6> clock mark (e.g. '2.45 pm') is preserved verbatim."""
    report = _report(fixtures_dir)
    transcript = " ".join(p for s in report.speeches for p in s.paragraphs)
    assert CLOCK_MARKER in transcript


def test_unpadded_date_to_iso(fixtures_dir: Path) -> None:
    """sittingDate '8-1-2025' parses to ISO date 2025-01-08 (Pitfall 6)."""
    report = _report(fixtures_dir)
    assert report.date == date(2025, 1, 8)


def test_transcript_sha256_stable(fixtures_dir: Path) -> None:
    """The SHA-256 fingerprint is 64 hex chars and stable across re-parses."""
    first = _report(fixtures_dir)
    second = _report(fixtures_dir)
    assert _SHA256_RE.fullmatch(first.transcript_sha256) is not None
    assert first.transcript_sha256 == second.transcript_sha256


def test_metadata_fields(fixtures_dir: Path) -> None:
    """Parl/Session/Sitting/Volume metadata is extracted from the payload."""
    report = _report(fixtures_dir)
    assert report.parliament_no == "14"
    assert report.session_no == "2"
    assert report.sitting_no == "149"
    assert report.volume == "95"
    assert report.report_id == BILL_REPORT_ID


def test_source_url_no_token(fixtures_dir: Path) -> None:
    """The provenance URL is the official SPRS sitting record, no token.

    sprs3 (post-2012) sits land on the SPA's #/fullreport route with the
    sitting date (the /hansard/<id> scheme 404s — SPRS is an SPA with no
    public per-report URL; verified live 2026-09-19).
    """
    report = _report(fixtures_dir)
    assert report.source_url == (
        "https://sprs.parl.gov.sg/search/#/fullreport?sittingdate=8-01-2025"
    )
    assert "hg_" not in report.source_url
