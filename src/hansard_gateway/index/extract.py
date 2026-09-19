"""Term extraction over sitting-TOC rows (spec §3.3).

Pure functions over the raw ``searchResult`` row dicts (the fixture shape):
no I/O, no settings. ``mpNames``/``htmlFileName`` are legitimately null on
recent (sprs3) rows — extraction treats those as "no value", not data loss.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

#: Term kinds (spec §3.3). Bills are the highest-value class.
KIND_BILL = "bill"
KIND_TOPIC = "topic"
KIND_SPEAKER = "speaker"
KIND_TYPE = "type"

#: Kind rank: a norm shared by several kinds keeps the best (lowest) rank —
#: a bill title that is also a topic stays a bill (spec §3.3 priority).
KIND_RANK: dict[str, int] = {
    KIND_BILL: 0,
    KIND_TOPIC: 1,
    KIND_SPEAKER: 2,
    KIND_TYPE: 3,
}
_RANK_KIND = {v: k for k, v in KIND_RANK.items()}

#: Words that never earn prefix rows of their own (spec §3.4). NOTE: "bill"
#: and "act" are deliberately NOT stopwords — they are how people search.
STOPWORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "of", "for", "on", "in", "to", "and"}
)

#: Minimum term length after normalisation; shorter terms are dropped.
MIN_TERM_LEN = 3

#: Bill-title pattern (spec §3.3): capture group is the bill name WITHOUT the
#: trailing "Bill" or its "(Amendment [No. N])" suffix. Matches
#: "Income Tax (Amendment) Bill" -> "Income Tax".
_BILL_RE = re.compile(
    r"^(.+?)(?:\s+\(Amendment(?:\s+No\.\s*\d+)?\))?\s+Bill\b",
    re.IGNORECASE,
)

#: Leading procedural noise stripped from topic titles (spec §3.3):
#: "Head B —", "Question No. 123", column marks, trailing colons.
_NOISE_RE = re.compile(
    r"^\s*(?:Head\s+[A-Z][\s\-–—]*|Question\s+No\.\s*\d+[\s\-–—:]*)+",
    re.IGNORECASE,
)

#: Character class kept by :func:`norm` (everything else is stripped).
_KEEP_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")

#: Field names on a raw searchResult row (spec row shape; RESEARCH §8).
_FIELD_TITLE = "title"
_FIELD_TYPE = "reportType"
_FIELD_SPEAKER = "mpNames"
_FIELD_SEEN = "sittingDate"

#: Sitting-date formats per era (sprs3 "8-1-2025", sprs2 "19-10-2004").
_DATE_FORMATS: tuple[str, ...] = ("%d-%m-%Y", "%d/%m/%Y")


@dataclass(frozen=True)
class ExtractedTerm:
    """One distinct searchable phrase with its display surface and coverage."""

    surface: str
    norm: str
    kind: str
    doc_count: int
    first_date: Optional[str] = None
    last_date: Optional[str] = None


def norm(value: str) -> str:
    """Normalise for dedup/lookup: NFKC, lowercase, strip outside [a-z0-9 ],
    collapse whitespace, trim (spec §3.3)."""
    lowered = unicodedata.normalize("NFKC", value).lower()
    kept = _KEEP_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", kept).strip()


def _iso_day(row: dict[str, Any]) -> Optional[str]:
    """Best-effort yyyy-mm-dd for a row's sitting date (None when absent)."""
    raw = str(row.get(_FIELD_SEEN) or "").strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def bill_name(title: str) -> Optional[str]:
    """Extract the bill name (sans trailing 'Bill') from a title, or None."""
    match = _BILL_RE.match(title.strip())
    if not match:
        return None
    return match.group(1).strip()


def topic_title(title: str) -> str:
    """The full title with leading procedural noise stripped (spec §3.3)."""
    cleaned = _NOISE_RE.sub("", title.strip())
    return _WS_RE.sub(" ", cleaned).strip()


def extract_terms(rows: list[dict[str, Any]]) -> list[ExtractedTerm]:
    """Extract all distinct terms (bill/topic/speaker/type) from TOC rows.

    Emits per report row: the bill name (kind=bill) when the title matches
    the spec bill regex, the noise-stripped title (kind=topic), the speaker
    (kind=speaker, skipped when null) and the report type (kind=type).
    Dedupes on ``norm`` keeping the most frequent surface and the strongest
    kind; drops terms shorter than :data:`MIN_TERM_LEN` or with doc_count 0.
    """
    # norm -> [doc_count, first_date, last_date, surface_counts, best_rank]
    acc: dict[str, list[Any]] = {}

    def _add(surface: str, kind: str, day: Optional[str]) -> None:
        clean = norm(surface)
        if not clean or len(clean) < MIN_TERM_LEN:
            return
        entry = acc.setdefault(clean, [0, None, None, {}, 99])
        entry[0] += 1
        entry[4] = min(entry[4], KIND_RANK[kind])
        if day:
            if entry[1] is None or day < entry[1]:
                entry[1] = day
            if entry[2] is None or day > entry[2]:
                entry[2] = day
        entry[3][surface] = entry[3].get(surface, 0) + 1

    for row in rows:
        day = _iso_day(row)
        title = str(row.get(_FIELD_TITLE) or "").strip()
        bill = bill_name(title) if title else None
        if bill:
            _add(bill, KIND_BILL, day)
        if title:
            _add(topic_title(title), KIND_TOPIC, day)
        speaker = row.get(_FIELD_SPEAKER)
        if speaker:
            _add(str(speaker), KIND_SPEAKER, day)
        report_type = row.get(_FIELD_TYPE)
        if report_type:
            _add(str(report_type), KIND_TYPE, day)

    terms: list[ExtractedTerm] = []
    for clean, (doc_count, first, last, surfaces, rank) in acc.items():
        if doc_count == 0:
            continue
        # Most frequent surface wins the display form (ties: longer surface).
        surface = max(surfaces, key=lambda s: (surfaces[s], -len(s)))
        terms.append(
            ExtractedTerm(
                surface=surface,
                norm=clean,
                kind=_RANK_KIND.get(rank, KIND_TOPIC),
                doc_count=doc_count,
                first_date=first,
                last_date=last,
            )
        )
    return sorted(terms, key=lambda t: (-t.doc_count, t.norm))
