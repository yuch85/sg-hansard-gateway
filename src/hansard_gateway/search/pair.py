"""Pair search provider — real backend-API adapter (D-04, 27-PAIR-SPIKE).

Route confirmed in the spike: ``POST https://search.pair.gov.sg/api/v1/search``
with a fixed ``X-Browser-ID`` UUID header (any UUID; 401 without one). Plain
httpx — no browser engine. Pair is discovery-only: its results are
normalization hits appended to the authoritative SPRS page, capped at
``pair_max_hits``. Host access is gated through the upstream allowlist.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

import httpx

from hansard_gateway.config import Settings
from hansard_gateway.models import SearchHit, SearchPage

logger = logging.getLogger(__name__)

#: Human date formats Pair returns (sample: "23 Mar 1972").
_HUMAN_DATE_FORMATS: tuple[str, ...] = ("%d %b %Y", "%d %B %Y")

#: Note text when the Pair upstream rejects or fails the request (T-27-19).
_PAIR_UNAVAILABLE_NOTE = (
    "Pair provider unavailable — results from SPRS only. "
    "See gateway logs for the upstream status."
)


def parse_pair_date(value: Optional[str]) -> Optional[date]:
    """Parse a Pair human-format date string (None when unparseable)."""
    if not value:
        return None
    from datetime import datetime

    for fmt in _HUMAN_DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


class PairSearchProvider:
    """httpx-backed Pair provider (never raises on the search path)."""

    def __init__(self, *, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._unavailable_reason: Optional[str] = None

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Last logged unavailability reason (None when the last call worked)."""
        return self._unavailable_reason

    async def search(
        self,
        *,
        query: str,
        date_from: Optional[date],
        date_to: Optional[date],
        speaker: Optional[str],
        page: int,
        limit: int,
    ) -> SearchPage:
        """One offset-paginated Pair search; degraded page on any failure."""
        url = f"https://{self._settings.pair_host}{self._settings.pair_api_path}"
        if self._settings.pair_host not in self._settings.upstream_host_allowlist:
            return self._unavailable_page(query, page, limit)
        body = self._search_body(query=query, date_from=date_from, date_to=date_to)
        body["offset"] = (page - 1) * self._settings.pair_max_hits
        try:
            response = await self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            logger.warning("pair upstream transport error", extra={"err": str(exc)})
            return self._unavailable_page(query, page, limit)
        if response.status_code != self._settings.pair_success_status:
            logger.warning(
                "pair upstream rejected request",
                extra={"status": response.status_code, "query": query},
            )
            return self._unavailable_page(query, page, limit)
        try:
            data = response.json()
        except ValueError as exc:
            logger.warning("pair upstream non-JSON body", extra={"err": str(exc)})
            return self._unavailable_page(query, page, limit)
        self._unavailable_reason = None
        total = self._total(data)
        results = data.get("searchResults", [])
        hits = [
            self._hit(row)
            for row in (results if isinstance(results, list) else [])
            if isinstance(row, dict)
        ]
        hits = [hit for hit in hits if hit is not None]
        return SearchPage(
            query=query,
            total=total,
            page=page,
            limit=self._settings.pair_max_hits,
            hits=hits,
            provider="pair",
            rendered_total=len(hits),
        )

    def _search_body(
        self, *, query: str, date_from: Optional[date], date_to: Optional[date]
    ) -> dict[str, Any]:
        """The spike-confirmed request body (all filter objects present)."""
        date_range: Any = "all"
        if date_from and date_to:
            date_range = {
                "startDate": date_from.isoformat(),
                "endDate": date_to.isoformat(),
            }
        return {
            "id": "",
            "hits": self._settings.pair_max_hits,
            "query": query,
            "offset": 0,
            "filters": {
                "dateRange": date_range,
                "hansardFilters": {},
                "caseJudgementFilters": {},
                "legislationFilters": {},
            },
            "sources": [self._settings.pair_source],
            "isLoggingEnabled": False,
        }

    @staticmethod
    def _total(data: Any) -> int:
        """metadata.numberOfResults (0 when absent or non-numeric)."""
        if not isinstance(data, dict):
            return 0
        meta = data.get("metadata")
        if not isinstance(meta, dict):
            return 0
        try:
            return int(meta.get("numberOfResults") or 0)
        except (TypeError, ValueError):
            return 0

    def _hit(self, row: dict[str, Any]) -> Optional[SearchHit]:
        """Normalize one Pair searchResults entry (trailing # stripped)."""
        raw_id = row.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            return None
        report_id = raw_id.rstrip("#")
        title = row.get("title")
        speaker = row.get("mpsSpeaking")
        if isinstance(speaker, list) and speaker:
            speaker = "; ".join(str(s) for s in speaker)
        else:
            speaker = None
        return SearchHit(
            report_id=report_id,
            link_id=report_id,
            date=parse_pair_date(row.get("date") if isinstance(row.get("date"), str) else None),
            title=str(title) if title else "(untitled)",
            report_type=row.get("reportTypeEnum") or row.get("reportType"),
            speaker=speaker,
            excerpt=row.get("snippet") if isinstance(row.get("snippet"), str) else None,
        )

    def _unavailable_page(
        self, query: str, page: int, limit: int
    ) -> SearchPage:
        """A logged, non-silent empty page (STYLE.md: no silent failures)."""
        self._unavailable_reason = _PAIR_UNAVAILABLE_NOTE
        return SearchPage(
            query=query,
            total=0,
            page=page,
            limit=limit,
            hits=[],
            provider="pair",
        )
