"""SPRS search provider — ports sgparl's sweep/dedupe algorithm (D-06).

SPRS is load-balanced across two backend nodes that disagree on totals and
orderings, so a single linear page walk drops rows. The algorithm:

1. Probe the first page up to `search_max_probes` times and keep the MAX
   observed `maxResult` (the two nodes report different totals).
2. Sweep 20-row pages, deduping rows by `reportId` in first-seen order, until
   the unique count reaches the probed max OR `search_max_no_gain` consecutive
   sweeps gain no new rows.

The upstream's "No Results Found" 500 is mapped to an empty result by
SprsClient, so an empty sweep is a terminal empty page (D-07), never a failure.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from hansard_gateway.config import Settings
from hansard_gateway.models import SearchHit, SearchPage
from hansard_gateway.sprs.client import SprsClient

logger = logging.getLogger(__name__)

#: Floor sitting date for rows whose date fields are missing.
_FALLBACK_DATE = date(1965, 1, 1)

#: Date formats per era: sprs2 (pre-2012) pads each component; sprs3 (post-2012)
#: does not (Pitfall 6 — "8-1-2025").
_DATES_SPRS2 = "%d-%m-%Y"
_DATES_SPRS3 = "%d-%m-%Y"

#: ReportVersion value marking the post-2012 silo.
_VERSION_SPRS3 = "sprs3"

#: Bounded length for the excerpt surfaced in search results.
_EXCERPT_MAX = 300


def parse_sitting_date(*, version: Optional[str], value: Optional[str]) -> date | None:
    """Parse an era-specific sittingDate into a date (None when unparseable)."""
    if not value:
        return None
    from datetime import datetime

    fmt = _DATES_SPRS3 if version == _VERSION_SPRS3 else _DATES_SPRS2
    try:
        return datetime.strptime(value.strip(), fmt).date()
    except ValueError:
        return None


def _as_int(value: Any) -> Optional[int]:
    """Best-effort int coercion of a numeric string field (None on failure)."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def normalize_row(row: dict[str, Any]) -> SearchHit:
    """Normalize one searchResult row into a SearchHit (D-02 link-id rule).

    `link_id` prefers `htmlFileName` (stable, spec-conformant, pre-2012) else
    the stripped live `reportId`. Rows lacking any usable id are dropped by the
    caller, which requires a non-empty `report_id`.
    """
    report_id = str(row.get("reportId") or "")
    html_file = row.get("htmlFileName")
    link_id = str(html_file) if html_file else report_id.rstrip("#")
    day = parse_sitting_date(
        version=row.get("reportVersion"), value=row.get("sittingDate")
    )
    if day is None:
        day = _component_date(
            from_day=row.get("fromDay"),
            from_month=row.get("fromMonth"),
            from_year=row.get("fromYear"),
        ) or _FALLBACK_DATE
    content = row.get("reportContent")
    excerpt: Optional[str] = None
    if isinstance(content, str) and content.strip():
        excerpt = " ".join(content.split())[:_EXCERPT_MAX]
    return SearchHit(
        report_id=report_id,
        link_id=link_id,
        date=day,
        title=str(row.get("title") or "(untitled)"),
        report_type=row.get("reportType"),
        speaker=row.get("mpNames"),
        excerpt=excerpt,
    )


def _component_date(
    *, from_day: Any, from_month: Any, from_year: Any
) -> Optional[date]:
    """Build a date from the sprs2 fromDay/fromMonth/fromYear components."""
    day = _as_int(from_day)
    month = _as_int(from_month)
    year = _as_int(from_year)
    if not day or not month or not year:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


class SprsSearchProvider:
    """SPRS searchResult sweep/dedupe provider (authoritative, D-06)."""

    def __init__(self, *, settings: Settings, client: SprsClient) -> None:
        self._settings = settings
        self._client = client

    async def search(
        self,
        *,
        query: str,
        date_from: date | None,
        date_to: date | None,
        speaker: str | None,
        page: int,
        limit: int,
        cold_budget: Optional[int] = None,
    ) -> SearchPage:
        """Sweep searchResult pages with dedupe and normalize to SearchPage.

        ``cold_budget`` (27.1-search-hop-rectify): when set, the sweep stops
        early once the probed maxResult shows the result set is larger than
        the budget — the returned page carries ``rendered_total`` = rows
        actually collected (the F-4 honest count) and ``total`` = the probed
        estimate. ``None`` = sweep to completion (small queries, the /date
        TOC sweep, warm-cache rebuilds).
        """
        lo = date_from or _FALLBACK_DATE
        hi = date_to or date.today()
        rows, total, budget_truncated = await self._sweep(
            keyword=query, date_from=lo, date_to=hi, mp_name=speaker or "",
            cold_budget=cold_budget,
        )
        start = (page - 1) * limit
        hits = [normalize_row(row) for row in rows[start : start + limit]]
        return SearchPage(
            query=query, total=total, page=page, limit=limit, hits=hits,
            provider="sprs", rendered_total=len(rows),
            truncated=budget_truncated,
        )

    async def sweep_for(
        self,
        *,
        query: str,
        date_from: date | None,
        date_to: date | None,
        speaker: str | None,
        cold_budget: Optional[int] = None,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Run the sweep only; return (deduped rows in first-seen order,
        probed maxResult, budget_truncated). The /search route caches the
        ROWS (page-agnostic — 27.1-search-hop-rectify) and slices them per
        page at render time via :func:`hansard_gateway.search.providers.
        select_page`. ``budget_truncated`` is True only when the cold page
        budget stopped the sweep early (rows remain uncollected); a no-gain
        short-sweep is NOT budget-truncated and keeps the legacy pagination."""
        lo = date_from or _FALLBACK_DATE
        hi = date_to or date.today()
        return await self._sweep(
            keyword=query, date_from=lo, date_to=hi, mp_name=speaker or "",
            cold_budget=cold_budget,
        )

    async def _sweep(
        self, *, keyword: str, date_from: date, date_to: date, mp_name: str,
        cold_budget: Optional[int] = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Run probe + sweep; return (deduped rows in first-seen order, total)."""
        page_size = self._settings.search_page_size
        probes = self._settings.search_max_probes
        # Cold-sweep budget (27.1-search-hop-rectify): once the probed max
        # shows the result set is LARGER than budget*page_size, stop the
        # sweep after `budget` fetches (the probes are part of the budget —
        # they are sweep fetches too). None = unlimited (small queries,
        # warm-cache rebuilds, and the /date TOC sweep keep sweeping to
        # completion).
        budget = (
            max(cold_budget, probes)
            if cold_budget is not None
            else None
        )
        total = 0
        seen: dict[str, dict[str, Any]] = {}
        no_gain = 0
        offset = 0
        fetches = 0
        budget_truncated = False
        while no_gain < self._settings.search_max_no_gain:
            rows = await self._client.search_result(
                keyword=keyword,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                mp_name=mp_name,
                start_index=offset,
                end_index=offset + page_size - 1,
            )
            fetches += 1
            if rows:
                total = max(total, self._max_result(rows))
            gained = 0
            for row in rows:
                key = str(row.get("reportId") or "")
                if key and key not in seen:
                    seen[key] = row
                    gained += 1
            no_gain = 0 if gained else no_gain + 1
            offset += page_size
            if fetches >= self._settings.search_max_probes and len(seen) >= total:
                break
            if offset >= max(total, self._settings.search_page_size):
                break
            # Budget stop is a genuine truncation only while the sweep was
            # still GAINING rows (no_gain == 0) — a stop in no-gain territory
            # is a false positive: the sweep was already winding down (e.g. a
            # static LB node repeating the same page) and would have
            # terminated on no-gain anyway, so it keeps the legacy pagination.
            if (budget is not None and fetches >= budget
                    and no_gain == 0 and len(seen) < total):
                budget_truncated = True
                break
        rows = list(seen.values())
        if len(rows) < total:
            logger.warning(
                "sweep ended short of probed max",
                extra={"got": len(rows), "total": total, "query": keyword,
                       "budget_truncated": budget_truncated},
            )
        return rows, total, budget_truncated

    @staticmethod
    def _max_result(rows: list[dict[str, Any]]) -> int:
        """The largest maxResult observed across a page of rows."""
        best = 0
        for row in rows:
            value = _as_int(row.get("maxResult"))
            if value:
                best = max(best, value)
        return best
