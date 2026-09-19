"""Synchronous httpx port of the SPRS searchResult contract for the crawl.

The upstream contract (21-field body, fixed headers, retry policy, the
"No Results Found" 500 -> empty mapping) has ONE source of truth:
``hansard_gateway.sprs.client``. This module imports those constants and
helpers rather than copying them (spec §3.1 / key_links).

The crawl enumerates single days (``keyword=""`` + a one-day dateRange), so
it replicates the sweep+dedupe algorithm from
``hansard_gateway.search.sprs.SprsSearchProvider`` (probe maxResult up to
``search_max_probes`` keeping the max; sweep ``search_page_size`` rows
deduped by reportId until no-gain) — the two LB nodes disagree on totals.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import date
from typing import Any, Optional

import httpx

from hansard_gateway.config import Settings, settings as _settings
from hansard_gateway.sprs.client import (
    _FIXED_HEADERS,
    _NO_RESULTS_MARKER,
    _RETRYABLE_STATUSES,
    build_search_body,
)

logger = logging.getLogger(__name__)

#: Target rate: one request per second, jittered (spec §3.1).
CRAWL_REQUEST_INTERVAL_S = 1.0
#: Fraction of the interval randomised per request (jitter, spec §3.1).
CRAWL_JITTER_FRACTION = 0.3

#: Endpoint path for the searchResult POST (relative to upstream_base).
_SEARCH_ENDPOINT = "/searchResult"


class CrawlTransientError(Exception):
    """Raised when a date's fetch fails after exhausting the retry budget.

    The caller (crawl_main) logs and SKIPS the date — it is NOT recorded as
    empty, so the next run retries it (spec §3.1; no silent failure).
    """

    def __init__(self, *, sitting: date, status: Optional[int], detail: str) -> None:
        super().__init__(
            f"crawl transient failure for {sitting.isoformat()}: "
            f"status={status} detail={detail}"
        )
        self.sitting = sitting
        self.status = status
        self.detail = detail


class CrawlClient:
    """Thin synchronous client for one-day sitting-TOC fetches."""

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._settings = settings or _settings
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            base_url = self._settings.upstream_base
            if base_url.startswith("/"):
                base_url = "http://localhost" + base_url
            self._client = httpx.Client(
                base_url=base_url,
                headers=_FIXED_HEADERS,
                timeout=httpx.Timeout(
                    connect=self._settings.connect_timeout_s,
                    read=self._settings.read_timeout_s,
                    write=self._settings.read_timeout_s,
                    pool=self._settings.connect_timeout_s,
                ),
            )

    def close(self) -> None:
        """Close the underlying httpx client if this instance owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "CrawlClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def search_sitting(self, *, sitting: date) -> list[dict[str, Any]]:
        """Fetch the full TOC for one sitting day (sweep + dedupe by reportId).

        Returns [] for non-sitting days (upstream 500 "No Results Found").
        Raises CrawlTransientError after the retry budget is exhausted.
        """
        day = sitting.isoformat()
        seen: dict[str, dict[str, Any]] = {}
        total = 0
        no_gain = 0
        offset = 0
        fetches = 0
        page_size = self._settings.search_page_size
        while no_gain < self._settings.search_max_no_gain:
            rows = self._fetch_page(
                keyword="",
                date_from=day,
                date_to=day,
                mp_name="",
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
            if (
                fetches >= self._settings.search_max_probes
                and len(seen) >= total
            ):
                break
            if offset >= max(total, page_size):
                break
        if seen and len(seen) < total:
            logger.warning(
                "crawl sweep for %s ended short of probed max",
                day,
                extra={"got": len(seen), "total": total},
            )
        return list(seen.values())

    def _fetch_page(
        self,
        *,
        keyword: str,
        date_from: str,
        date_to: str,
        mp_name: str,
        start_index: int,
        end_index: int,
    ) -> list[dict[str, Any]]:
        """One searchResult page with the retry policy; [] on empty 500."""
        body = build_search_body(
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
            mp_name=mp_name,
            start_index=start_index,
            end_index=end_index,
        )
        attempts = self._settings.retry_attempts
        for attempt in range(attempts):
            try:
                response = self._client.post(_SEARCH_ENDPOINT, json=body)
            except httpx.TransportError as exc:
                if attempt == attempts - 1:
                    raise CrawlTransientError(
                        sitting=date.fromisoformat(date_from),
                        status=None,
                        detail=f"transport: {exc}",
                    ) from exc
                self._sleep_backoff(attempt)
                continue
            status = response.status_code
            if status == self._settings.http_internal_error:
                if _NO_RESULTS_MARKER in response.text:
                    return []  # EMPTY, not a failure (RESEARCH pitfall 3)
            elif status not in _RETRYABLE_STATUSES:
                try:
                    data = response.json()
                except ValueError:
                    data = None
                return self._rows(data)
            self._sleep_backoff(attempt)
        raise CrawlTransientError(
            sitting=date.fromisoformat(date_from),
            status=status,
            detail="retry budget exhausted",
        )

    def _sleep_backoff(self, attempt: int) -> None:
        """Jittered sleep before the next retry / page fetch."""
        base = self._settings.retry_backoff_s * (attempt + 1)
        sleep_s = base + random.uniform(
            0, CRAWL_JITTER_FRACTION * CRAWL_REQUEST_INTERVAL_S
        )
        time.sleep(sleep_s)

    @staticmethod
    def _rows(data: Any) -> list[dict[str, Any]]:
        """Normalize a decoded body into a list of row dicts (or [])."""
        if isinstance(data, dict):
            return [v for v in data.values() if isinstance(v, dict)]
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []

    @staticmethod
    def _max_result(rows: list[dict[str, Any]]) -> int:
        """Largest maxResult observed across a page of rows (nodes disagree)."""
        best = 0
        for row in rows:
            try:
                value = int(str(row.get("maxResult") or "0").strip() or "0")
            except ValueError:
                continue
            best = max(best, value)
        return best
