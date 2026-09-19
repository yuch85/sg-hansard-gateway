"""sprs3 (post-2012) topic parser — verbatim transcript extraction.

The post-2012 upstream payload is a raw HTML FRAGMENT in
``resultHTML.content`` (no ``<html>`` wrapper, no meta tags — RESEARCH Finding 3 /
A6, VERIFIED on the bill-742 fixture). Speakers are ``<strong>`` prefixes inside
``<p>`` tags; paragraphs without a ``<strong>`` carry forward the last speaker
(within this one fragment only — never merged across topics). Consecutive
same-speaker runs are merged. ``<h6>`` clock marks and ``(proc text)`` markers
are preserved verbatim (spec §14, Pitfall 8).

Metadata comes from the ``resultHTML`` object itself (``parlNo``, ``sessionNo``,
``sittingNo``, ``volumeNo``, ``sittingDate``, ``reportType``, ``title``).
``sittingDate`` is UNPADDED D-M-YYYY (e.g. ``8-1-2025``) — parsed with
``%d-%m-%Y`` and re-emitted as ISO (Pitfall 6).
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any, Optional, Sequence

from bs4 import BeautifulSoup

from hansard_gateway.models import HansardReport, Speech
from hansard_gateway.sprs.payload import TopicPayload

#: ``sittingDate`` arrives unpadded (``8-1-2025``); ``%d-%m-%Y`` accepts it.
SITTING_DATE_FORMAT = "%d-%m-%Y"

#: Minimum paragraph length before a walk is considered substantive.
_MIN_PARAGRAPH_CHARS = 1


def _parse_sitting_date(raw: str) -> date:
    """Parse the unpadded ``sittingDate`` (D-M-YYYY) into an ISO ``date``."""
    return datetime.strptime(raw, SITTING_DATE_FORMAT).date()


def _paragraph_text(p: Any) -> str:
    """Whitespace-collapse a ``<p>``'s text, replacing NBSP, dropping a leading ``:``.

    Keeps ``(proc text)`` markers verbatim; only normalises whitespace.
    """
    text = p.get_text(" ", strip=True)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.lstrip(":").strip()


def _clock_text(node: Any) -> str:
    """Whitespace-collapse an ``<h6>`` clock mark (verbatim time preserved)."""
    text = node.get_text(" ", strip=True).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _walk_speeches(fragment: str) -> list[Speech]:
    """Walk ``<p>``/``<h6>`` nodes into merged, same-speaker Speech runs.

    A ``<p>`` with a ``<strong>`` starts a new speaker run (text after the
    leading ``:``); a ``<p>`` without one carries forward the last speaker.
    An ``<h6>`` clock mark is appended verbatim to the current run (or becomes
    a procedural run when no speaker is active yet).
    """
    soup = BeautifulSoup(fragment, "lxml")
    speeches: list[Speech] = []
    current_speaker: Optional[str] = None

    def _append(text: str, speaker: Optional[str]) -> None:
        nonlocal current_speaker
        if not text:
            return
        if (
            speaker is not None
            and speeches
            and speeches[-1].speaker_original == speaker
        ):
            speeches[-1].paragraphs.append(text)
            return
        current_speaker = speaker
        speeches.append(
            Speech(
                sequence=len(speeches) + 1,
                speaker_original=speaker,
                speaker_name=None,
                speaker_role=None,
                paragraphs=[text],
            )
        )

    for node in soup.find_all(["p", "h6"]):
        if node.name == "h6":
            _append(_clock_text(node), current_speaker)
            continue
        strong = node.find("strong")
        if strong is not None:
            speaker = strong.get_text(" ", strip=True)
            # Text of the paragraph AFTER the speaker prefix (drop the colon).
            rest = node.get_text(" ", strip=True)
            prefix = strong.get_text(" ", strip=True)
            text = rest[len(prefix):].lstrip(":").strip()
        else:
            speaker = current_speaker
            text = _paragraph_text(node)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < _MIN_PARAGRAPH_CHARS:
            continue
        _append(text, speaker)
    return speeches


def _transcript_sha256(speeches: Sequence[Speech]) -> str:
    """SHA-256 of the normalized transcript (deterministic join order)."""
    lines = [
        paragraph
        for speech in speeches
        for paragraph in speech.paragraphs
    ]
    normalized = "\n".join(lines)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse(
    payload: TopicPayload, *, report_id: str
) -> HansardReport:
    """Parse an sprs3 ``content`` fragment into a populated HansardReport.

    Computes the verbatim ``transcript_sha256`` fingerprint (D-16) and sets
    ``source_url`` to the official SPRS record URL.
    """
    fragment = payload.content
    if not fragment:
        raise ValueError("sprs3 payload has no content")

    speeches = _walk_speeches(fragment)

    try:
        sit_date = _parse_sitting_date(payload.sitting_date)
    except (ValueError, TypeError):
        sit_date = date.min

    report = HansardReport(
        report_id=report_id,
        date=sit_date,
        title=payload.title or "",
        topic_type=payload.report_type,
        source_url=payload.source_url,
        volume=payload.volume_no,
        parliament_no=payload.parl_no,
        session_no=payload.session_no,
        sitting_no=payload.sitting_no,
        speeches=speeches,
        transcript_sha256=_transcript_sha256(speeches),
    )
    return report
