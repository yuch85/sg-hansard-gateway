"""Shared SPRS httpx upstream client (D-01: one client, both POST endpoints).

Sends ONLY the fixed browser-like header set (RESEARCH Finding 2) — it never
copies the caller's headers/cookies and never forwards the capability token
(addendum §19). A host allowlist guards against SSRF (addendum §20). The
retry policy (D-07) maps the upstream's "No Results Found" 500 to an empty
result and retries transient 400/502/503/504/connection-reset, but NOT 404 or
parse failures.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from hansard_gateway.config import Settings, settings as _settings
from hansard_gateway.rate_limit import RateGate
from hansard_gateway.report_id import normalize_for_topic

#: HTTP statuses the upstream uses for transient failures (D-07), from Settings.
_RETRYABLE_STATUSES = frozenset(
    {
        _settings.http_bad_request,
        _settings.http_bad_gateway,
        _settings.http_unavailable,
        _settings.http_gateway_timeout,
    }
)

#: Exact body substring marking the "empty result" 500 (NOT a failure, D-07).
_NO_RESULTS_MARKER = "No Results Found"

#: Fixed header set — the ONLY headers sent upstream (addendum §19). Built from
#: Settings so the exact browser UA string lives in one place (config.py).
_FIXED_HEADERS = {
    "User-Agent": _settings.upstream_user_agent,
    "Content-Type": _settings.upstream_content_type,
    "Referer": _settings.upstream_referer,
}

#: The getHansardTopic wrapper field carrying the result dict (sprs3 shape).
_RESULT_HTML_FIELD = "resultHTML"

#: Top-level fields carrying the transcript in a flat (unwrapped) payload:
#: sprs2 full document (``htmlContent``) / sprs3 fragment (``content``).
_TRANSCRIPT_FIELDS: tuple[str, ...] = ("htmlContent", "content")


# The 21-field searchResult body builders live in :mod:`.body` (300-LOC
# split, Phase 27.1 wave 6, F-5b); re-exported here so the offline crawl
# (crawl/crawl_client.py imports build_search_body from this module) and any
# existing ``from ...sprs.client import build_search_body`` keep working.
from hansard_gateway.sprs.body import (  # noqa: F401
    build_search_body,
    format_day_range,
)


def _has_transcript(data: dict[str, Any]) -> bool:
    """True if a flat payload carries a non-empty transcript string field."""
    return any(
        isinstance(data.get(field), str) and data.get(field)
        for field in _TRANSCRIPT_FIELDS
    )


class UpstreamError(Exception):
    """Raised when the upstream fails after exhausting the retry budget."""

    def __init__(self, *, status: Optional[int], detail: str) -> None:
        super().__init__(f"upstream error: status={status} detail={detail}")
        self.status = status
        self.detail = detail


class SprsClient:
    """httpx client for the two live SPRS POST endpoints."""

    def __init__(
        self,
        *,
        settings: Settings,
        gate: RateGate,
        token_label: str,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._settings = settings
        self._gate = gate
        self._token_label = token_label
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.upstream_base,
            headers=_FIXED_HEADERS,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_s,
                read=settings.upstream_attempt_timeout_s,
                write=settings.upstream_attempt_timeout_s,
                pool=settings.connect_timeout_s,
            ),
        )

    async def aclose(self) -> None:
        """Close the underlying httpx client if this instance owns it."""
        if self._owns_client:
            await self._client.aclose()

    def host_allowlisted(self, *, url: str) -> bool:
        """Return True if `url`'s host is in the configured allowlist (SSRF guard)."""
        host = httpx.URL(url).host
        return host in self._settings.upstream_host_allowlist

    async def search_result(
        self,
        *,
        keyword: str,
        date_from: str,
        date_to: str,
        mp_name: str,
        start_index: int,
        end_index: int,
    ) -> list[dict[str, Any]]:
        """POST searchResult and return the row array (empty on no-results 500)."""
        body = self._search_body(
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
            mp_name=mp_name,
            start_index=start_index,
            end_index=end_index,
        )
        data, status = await self._post("searchResult", body)
        if status == _settings.http_internal_error and _NO_RESULTS_MARKER in self._body_text(data):
            return []
        return self._rows(data)

    async def fetch_topic(self, *, report_id: str) -> dict[str, Any]:
        """POST getHansardTopic (trailing # stripped) and return the resultHTML.

        Tolerates BOTH upstream envelope shapes (live drift): a ``resultHTML``
        dict wrapper (sprs3, e.g. bill-742) and a flat top-level payload
        (sprs2, e.g. the 2004 SAF report) whose ``htmlContent``/``content``
        field carries the transcript. A payload with neither raises
        UpstreamError (fail-closed — the app maps it to 502, never fabricates).
        """
        body = {"id": normalize_for_topic(report_id=report_id)}
        data, status = await self._post("getHansardTopic", body)
        if status == _settings.http_internal_error \
                and _NO_RESULTS_MARKER in self._body_text(data):
            # A 500 no-results body = the id is well-formed but the corpus
            # does not hold it: spec §6.2's valid-token 404 (main.py maps
            # exc.status == 404 to the nav-bar 404).
            raise UpstreamError(
                status=_settings.http_not_found, detail="report not found")
        result = data.get(_RESULT_HTML_FIELD) if isinstance(data, dict) else None
        if isinstance(result, dict):
            return result
        if isinstance(data, dict) and _has_transcript(data):
            return data
        raise UpstreamError(status=None, detail="resultHTML missing")

    def _search_body(
        self,
        *,
        keyword: str,
        date_from: str,
        date_to: str,
        mp_name: str,
        start_index: int,
        end_index: int,
    ) -> dict[str, Any]:
        """Build the full 21-field searchResult body (Finding 2)."""
        return build_search_body(
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
            mp_name=mp_name,
            start_index=start_index,
            end_index=end_index,
        )

    def _rows(self, data: Any) -> list[dict[str, Any]]:
        """Normalize the response into a list of row dicts (array or object)."""
        if isinstance(data, dict):
            return [v for v in data.values() if isinstance(v, dict)]
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        raise UpstreamError(status=None, detail="unexpected searchResult shape")

    @staticmethod
    def _body_text(data: Any) -> str:
        """Best-effort string form of a decoded body for marker matching."""
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            return str(data)
        return ""

    async def _post(self, endpoint: str, body: dict[str, Any]) -> tuple[Any, int]:
        """POST `endpoint` through the rate gate + retry policy; return (data, status).

        Returns the decoded JSON (or raw text for a 500 no-results body) and the
        HTTP status, so callers can inspect the status without re-raising.
        """
        rel = f"/{endpoint}"
        # SSRF guard: the only host we ever reach is the configured upstream base.
        if not self.host_allowlisted(url=self._settings.upstream_base):
            raise UpstreamError(status=None, detail="upstream base host not allowlisted")
        attempts = self._settings.retry_attempts
        last_status: Optional[int] = None
        last_data: Any = None
        for attempt in range(attempts):
            try:
                data, status = await self._post_once(url=rel, body=body)
            except UpstreamError as exc:
                # F-5b (wave 6): a transport-level failure — a connect/read
                # stall past the per-call timeout surfaces as a
                # ``httpx.TransportError`` (ConnectTimeout included) that
                # ``_post_once`` wraps in UpstreamError(status=None). Without
                # catching it HERE it escapes the retry loop entirely (the
                # loop only retried HTTP-status failures), so a stall burned
                # ZERO retries and hung past the client's fetch timeout.
                # Catching it lets the stall burn the full retry budget +
                # backoff, then degrade to a bounded 5xx-with-nav.
                if attempt < attempts - 1:
                    await asyncio.sleep(self._settings.retry_backoff_s)
                    continue
                raise exc
            last_status, last_data = status, data
            if status not in _RETRYABLE_STATUSES:
                return data, status
            if attempt < attempts - 1:
                await asyncio.sleep(self._settings.retry_backoff_s)
        raise UpstreamError(status=last_status, detail="retry budget exhausted")

    async def _post_once(
        self, *, url: str, body: dict[str, Any]
    ) -> tuple[Any, int]:
        """A single POST attempt under the global + per-token upstream gate.

        F-5 (wave 6): each attempt is capped by the per-attempt timeout
        (``settings.upstream_attempt_timeout_s``) via a per-call
        ``timeout=`` override — an upstream that stalls past the cap
        degrades to a fast 5xx (the existing retry budget + backoff are
        unchanged, but no single attempt can hang for the 20s read
        timeout or longer).
        """
        slot = self._gate.acquire_upstream(token_label=self._token_label)
        result = await slot.__aenter__()
        if not result.admitted:
            raise UpstreamError(status=result.status, detail=result.reason)
        try:
            response = await self._client.post(
                url, json=body,
                timeout=httpx.Timeout(
                    connect=self._settings.connect_timeout_s,
                    read=self._settings.upstream_attempt_timeout_s,
                    write=self._settings.upstream_attempt_timeout_s,
                    pool=self._settings.connect_timeout_s,
                ),
            )
            if response.status_code == _settings.http_internal_error:
                return response.text, response.status_code
            return response.json(), response.status_code
        except httpx.TransportError as exc:
            raise UpstreamError(status=None, detail=f"transport: {exc}") from exc
        finally:
            await slot.__aexit__(None, None, None)
