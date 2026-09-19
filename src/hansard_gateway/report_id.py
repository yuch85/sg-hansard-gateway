"""Report-ID validation, date derivation, normalization, resolution planning.

Two ID systems coexist upstream (D-02, D-03): the spec-style `htmlFileName`
(`037_20041019_S0004_T0023`) and the live per-era `reportId`
(`bill-742`, `00075526-WA.00070465-ZZ_1`). `/report/{id}` must accept both,
reject path traversal, and plan a resolution path (direct topic fetch vs
date-scoped browse fallback).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

#: Permissive charset/length guard (spec §18). Deliberately NOT tightened to the
#: spec grammar only — live `reportId` values like `bill-742` must pass (D-03).
_REPORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

#: Any 8-digit run inside an ID is a candidate YYYYMMDD sitting date.
_DATE_SEG_RE = re.compile(r"\d{8}")


class ResolutionStrategy(str, Enum):
    """Which resolution path the gateway takes for a validated ID."""

    DIRECT_TOPIC = "direct_topic"
    DATE_BROWSE = "date_browse"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class ResolutionPlan:
    """The resolution path for a validated report ID.

    `candidate_ids` are the IDs to try against `getHansardTopic` in order;
    `browse_date` is set only for the date-scoped `searchResult` fallback.
    """

    strategy: ResolutionStrategy
    candidate_ids: list[str] = field(default_factory=list)
    browse_date: date | None = None


def validate_report_id(*, report_id: str) -> bool:
    """Return True if the ID is traversal-safe and within the allowed charset.

    Rejects `/`, `..`, `#` (any position — callers pass raw path params;
    normalization happens separately for known live IDs), and any char outside
    ``[A-Za-z0-9_-]``, as well as IDs longer than 100 chars.
    """
    return _REPORT_ID_RE.fullmatch(report_id) is not None


def derive_date(*, report_id: str) -> date | None:
    """Extract the YYYYMMDD sitting date from an 8-digit segment, if present.

    Returns None when the ID carries no date (live `reportId` era) — the date
    then comes from the fetched payload (D-03). Live row IDs can contain
    non-date 8-digit runs (e.g. the WA ref `00066597`); we select the first
    run that parses as a real calendar date rather than the first run.
    """
    for match in _DATE_SEG_RE.finditer(report_id):
        raw = match.group(0)
        try:
            return date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError:
            continue
    return None


def normalize_for_topic(*, report_id: str) -> str:
    """Strip trailing `#` chars so the ID is accepted by `getHansardTopic`.

    Live row IDs carry a trailing `#`/`##` which makes the upstream 400
    (D-01, Pitfall 5). Interior `#` are never produced by validated IDs.
    """
    return report_id.rstrip("#")


def resolution_plan(*, report_id: str) -> ResolutionPlan:
    """Plan how to resolve a validated ID to a fetched topic.

    Direct topic fetch for spec-grammar IDs and `#`-stripped live IDs (the
    direct fetch always runs first); a date-scoped browse fallback when a date
    is derivable from the ID; otherwise not found.
    """
    if not validate_report_id(report_id=report_id):
        return ResolutionPlan(strategy=ResolutionStrategy.NOT_FOUND)
    candidates = [report_id]
    stripped = normalize_for_topic(report_id=report_id)
    if stripped != report_id:
        candidates.append(stripped)
    browse_date = derive_date(report_id=report_id)
    if browse_date is None:
        return ResolutionPlan(
            strategy=ResolutionStrategy.DATE_BROWSE,
            candidate_ids=candidates,
        )
    return ResolutionPlan(
        strategy=ResolutionStrategy.DIRECT_TOPIC,
        candidate_ids=candidates,
        browse_date=browse_date,
    )
