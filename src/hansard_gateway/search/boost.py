"""Exact-term search boost (F-4, wave 6).

When the normalised query is an EXACT term in the local index (NFKC, via
``index.extract.norm``), the reports indexed under that term are PINNED to the
top of the rendered result page (a metadata join on the index DB — no
transcript fetch, per the index's metadata-only invariant). Without this, an
exact-name query like "Health Information Bill" ranks behind SPRS full-text
noise and the HIB reports land on page 3, not page 1.

Ranked (non-pinned) boost: when the query is a strict prefix (word-by-word)
of exactly ONE indexed term, that term's reports are appended after the
upstream hits in index order. Threshold (documented): the prefix must cover
the query's words in order AND match exactly one term — with multiple
candidates (e.g. 'health' -> 'health information' AND 'health information
bill') the boost is skipped rather than guessing.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from hansard_gateway.index.extract import norm
from hansard_gateway.models import SearchHit, SearchPage

_LOG = logging.getLogger(__name__)

#: Date formats the index stores (yyyy-mm-dd); the sweep's sprs2/sprs3 forms
#: are already ISO by the time rows reach the index.
_INDEX_DATE_FMT = "%Y-%m-%d"

#: A report whose index title matches the boosted query term, verbatim
#: (case-insensitive) — pinned even when upstream hit ordering disagrees.
_EXACT_TITLE_MATCH = "exact"

#: A report indexed under the boosted term whose title was not returned by
#: the upstream page (or returned under a different surface) — appended.
_TITLE_VARIATION = "variation"


def _parse_index_day(value: Optional[str]) -> Optional[date]:
    """ISO yyyy-mm-dd from the index into a date (None when absent/bad)."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _hit_from_index_row(row: tuple[str, str, str, Optional[str], Optional[str]]) -> SearchHit:
    """One index (link_id, title, date, report_type, speaker) row as a hit."""
    link_id, title, day_iso, report_type, speaker = row
    return SearchHit(
        report_id=link_id,
        link_id=link_id,
        date=_parse_index_day(day_iso),
        title=title,
        report_type=report_type,
        speaker=speaker,
        excerpt=None,
    )


def _prefix_terms(index, query: str) -> list[tuple[str, str]]:
    """Indexed terms whose norm has the query's word sequence as a prefix.

    Returns (surface, doc_count) pairs; empty when no word of the query
    exists in the index (the index_words set is built once, cache-backed).
    """
    words = [w for w in norm(query).split(" ") if w]
    if not words:
        return []
    index_words: set[str] = set()
    for _surface, tnorm, _doc in index.terms_by_norm():
        index_words.update(tnorm.split(" "))
    if words[0] not in index_words:
        return []
    candidates: list[tuple[str, str]] = []
    for surface, tnorm, doc_count in index.terms_by_norm():
        twords = tnorm.split(" ")
        if twords[: len(words)] == words:
            candidates.append((surface, doc_count))
    return candidates


def apply_exact_term_boost(
    *, page: SearchPage, index, query: str
) -> tuple[SearchPage, Optional[str]]:
    """Pin / rank-boost the reports of an exact (or unique-prefix) indexed
    term; returns (new page, note) where note is None when no boost applied.

    The boost never DROPS upstream hits — index rows are prepended (pinned)
    or appended (ranked) and de-duplicated by link_id. ``page.total`` is
    left untouched (it is the upstream estimate; the header's honesty comes
    from ``rendered_total`` instead).
    """
    nquery = norm(query)
    if not nquery:
        return page, None
    term = index.term_by_norm(nquery)
    mode: Optional[str] = None
    if term is not None:
        mode = _EXACT_TITLE_MATCH
    else:
        candidates = _prefix_terms(index, query)
        if len(candidates) == 1:
            mode = _TITLE_VARIATION
            term = (candidates[0][0], candidates[0][1])
    if term is None:
        return page, None

    surface, _doc_count = term
    rows = index.reports_for_norm(norm(surface))
    if not rows:
        return page, None

    hits = list(page.hits)
    seen = {h.link_id for h in hits}
    boosted = 0
    if mode == _EXACT_TITLE_MATCH:
        pinned: list[SearchHit] = []
        for row in rows:
            hit = _hit_from_index_row(row)
            if hit.link_id not in seen:
                pinned.append(hit)
                seen.add(hit.link_id)
                boosted += 1
        hits = pinned + hits
    else:
        for row in rows:
            hit = _hit_from_index_row(row)
            if hit.link_id not in seen:
                hits.append(hit)
                seen.add(hit.link_id)
                boosted += 1

    if boosted == 0:
        return page, None
    note = (
        f"Indexed reports for the exact term “{surface}” "
        f"({boosted} shown) are pinned to the top of this page."
        if mode == _EXACT_TITLE_MATCH
        else f"Indexed reports for “{surface}” (the closest indexed term) "
        f"({boosted} shown) are listed after the upstream results."
    )
    _LOG.info(
        "exact-term boost applied",
        extra={"mode": mode, "surface": surface, "boosted": boosted,
               "query": query},
    )
    return SearchPage(
        query=page.query,
        total=page.total,
        page=page.page,
        limit=page.limit,
        hits=hits,
        provider=page.provider,
        rendered_total=page.rendered_total,
    ), note
