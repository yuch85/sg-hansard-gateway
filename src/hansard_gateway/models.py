"""Normalized internal data models (spec §8, D-14).

Plain frozen dataclasses — NOT Pydantic. Pydantic is reserved for API-boundary
query-param validation only (STYLE.md Rule 13). The rendering and JSON layers
consume these models; nothing downstream should see raw SPRS payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Speech:
    """One contiguous run of transcript paragraphs by a single speaker.

    `speaker_original` preserves the source text verbatim (legal record,
    spec §14). `speaker_name` / `speaker_role` are normalized splits filled in
    by the parser layer (Plan 03) when unambiguous.
    """

    sequence: int
    speaker_original: str | None
    speaker_name: str | None
    speaker_role: str | None
    paragraphs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HansardReport:
    """A single normalized Hansard report (one topic, one sitting)."""

    report_id: str
    date: date
    title: str
    topic_type: str | None
    source_url: str
    volume: str | None
    parliament_no: str | None
    session_no: str | None
    sitting_no: str | None
    speeches: list[Speech] = field(default_factory=list)
    #: SHA-256 of the normalized transcript, computed by the parser layer (D-16).
    transcript_sha256: str = ""


@dataclass(frozen=True)
class SearchHit:
    """One normalized row of a search-result page."""

    report_id: str
    link_id: str
    date: date | None
    title: str
    report_type: str | None
    speaker: str | None
    excerpt: str | None


@dataclass(frozen=True)
class SearchPage:
    """A normalized page of search results from one provider.

    ``total`` is the upstream-observed total (an ESTIMATE — the SPRS
    backend nodes disagree on totals, so it is a per-page probe maximum,
    never a true count; F-4). ``rendered_total`` is the exact number of
    hits this page's provider actually retrieved and is the honest
    count for the results header.
    """

    query: str
    total: int
    page: int
    limit: int
    hits: list[SearchHit] = field(default_factory=list)
    provider: str = ""
    #: Exact number of hits retrieved by the provider for this page's
    #: query (F-4 honest header); 0 = unknown (legacy callers).
    rendered_total: int = 0
    #: True when the sweep was bounded to the cold page budget and did NOT
    #: collect the full probed result set (27.1-search-hop-rectify). Drives
    #: the honest truncated note + the pagination cap (continuation links
    #: span only the collected rows). A complete sweep is never truncated.
    truncated: bool = False
