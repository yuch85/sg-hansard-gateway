"""The /report/{id} handler (extracted from main.py for the 300-LOC rule).

Phase 27.1 wave 4: the handler grew with the spec §6.2 404 split (a
well-formed id the corpus does not hold → 404 with the nav bar + recovery,
distinct from the byte-identical invalid-token 404). Kept in its own module
so main.py stays under the 300-LOC cap.
"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import Depends, Request, Response
from fastapi.responses import HTMLResponse

from hansard_gateway.auth import AuthContext, require_capability_token
from hansard_gateway import error_responses, render
from hansard_gateway.config import Settings, settings
from hansard_gateway.openapi_schemas import report_responses
from hansard_gateway.render.links import build_report_nav
from hansard_gateway.sprs.cache import ReportCache
from hansard_gateway.sprs.client import SprsClient, UpstreamError
from hansard_gateway.sprs.payload import from_result_html, parse_topic, topic_source_url
from hansard_gateway.report_id import validate_report_id

#: Allowed ``format`` query values (spec §12); HTML is the default.
_FORMATS: frozenset[str] = frozenset({"html", "json", "text"})

#: Response headers for protected (token-bearing) responses (addendum §11-13).
_PROTECTED_HEADERS: dict[str, str] = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}

#: The ISO timestamp format for the provenance "Retrieved" line (spec §13).
_RETRIEVED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _sitting_iso(*, result_html: dict) -> str:
    """Sitting date of a raw ``resultHTML`` payload as ISO, or ``date.min``.

    Both eras carry the sitting date (sprs2 ``<meta Sit_Date>`` / sprs3
    top-level ``sittingDate``, D-M-YYYY unpadded). The provenance URL needs
    it to point at the correct SPRS sitting route; a missing/unparseable
    date degrades to ``date.min`` (ISO) — the pre-2013 route, which matches
    every real pre-2013 report — never a fabricated value.
    """
    from datetime import date, datetime

    raw = str(result_html.get("sittingDate") or "").strip()
    if not raw:
        # sprs2: the date lives in the full-document <meta> tags.
        html = str(result_html.get("htmlContent") or "")
        match = re.search(
            r'name="Sit_Date"\s+content="([^"]*)"', html, re.IGNORECASE
        )
        raw = match.group(1).strip() if match else ""
    if not raw:
        return date.min.isoformat()
    # sprs3 ships unpadded D-M-YYYY; sprs2 meta is ISO YYYY-MM-DD.
    try:
        return datetime.strptime(raw, "%d-%m-%Y").date().isoformat()
    except ValueError:
        pass
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return date.min.isoformat()


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 Zulu string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime(_RETRIEVED_FORMAT)


def _format_response(body: str, *, fmt: str) -> Response:
    """Wrap a serialized body with the correct media type (spec §12)."""
    from fastapi.responses import PlainTextResponse

    if fmt == "json":
        return Response(content=body, media_type="application/json",
                        headers=_PROTECTED_HEADERS)
    return PlainTextResponse(body, headers=_PROTECTED_HEADERS)

def register_report_route(protected, *, cfg: Settings, gate, cache, index) -> None:
    """Mount /report/{report_id} on the shared protected router."""
    @protected.get("/report/{report_id}", responses=report_responses())
    async def report(
        request: Request,
        report_id: str,
        format: str = "html",
        ctx: AuthContext = Depends(require_capability_token),
    ) -> Response:
        """Retrieve ONE Hansard report (a full sitting transcript segment).

        ``report_id`` is either a spec-style id (``037_20041019_S0004_T0023``)
        or a live reportId (``bill-742``). Returns the verbatim transcript —
        speaker by speaker — with a SHA-256 integrity footer. Use
        ``format=json`` for structured data, ``text`` for plain text.
        """
        if format not in _FORMATS:
            return error_responses.build_error_response(
                status=settings.http_unprocessable,
                code=error_responses.CODE_INVALID_PARAMETER,
                fmt="html",
                token=ctx.token,
                report_id=report_id,
            )
        if not validate_report_id(report_id=report_id):
            return error_responses.build_error_response(
                status=settings.http_unprocessable,
                code=error_responses.CODE_INVALID_PARAMETER,
                fmt=format,
                token=ctx.token,
                report_id=report_id,
                detail=f"“{report_id}” is not a valid report id (letters, "
                       f"digits, '-' and '_' only, up to 100 characters).",
            )

        key = cache.report_key(report_id=report_id)
        cached = cache.get(key=key)
        # Stash for the request log (middleware reads scope.state): True when
        # the transcript came from the in-process TTL cache, False on a miss,
        # and the upstream HTTP status on the failure path (2026-09-18 diag).
        request.scope["state"]["cache_hit"] = cached is not None
        if cached is None:
            client = SprsClient(settings=cfg, gate=gate, token_label=ctx.token_label)
            try:
                result_html = await client.fetch_topic(report_id=report_id)
            except UpstreamError as exc:
                request.scope["state"]["upstream_status"] = exc.status
                # A 404 from SPRS = a well-formed id the corpus does not
                # hold: spec §6.2's valid-token 404 (nav bar + recovery).
                if exc.status == settings.http_not_found:
                    return error_responses.build_error_response(
                        status=settings.http_not_found,
                        code=error_responses.CODE_NOT_FOUND,
                        fmt=format,
                        token=ctx.token,
                        report_id=report_id,
                        detail=f"No Hansard report matches “{report_id}” — "
                               f"use the Find-a-topic A-Z ladder or the "
                               f"year index to locate the right one.",
                    )
                from hansard_gateway.main import _error_response

                return _error_response(report_id, ctx.token, exc, fmt=format)
            finally:
                await client.aclose()
            payload = from_result_html(
                result_html,
                source_url=topic_source_url(
                    report_id=report_id,
                    sitting_date_iso=_sitting_iso(result_html=result_html),
                ),
            )
            try:
                cached = parse_topic(payload, report_id=report_id)
            except ValueError as exc:
                # Unparseable payload → 502 (spec §17), never fabricate text.
                from hansard_gateway.main import _error_response

                return _error_response(
                    report_id, ctx.token,
                    UpstreamError(status=settings.http_bad_gateway, detail=str(exc)),
                    fmt=format,
                )
            cache.set(key=key, value=cached)

        if format != "html":
            return _format_response(
                render.render_report_format(report=cached, fmt=format, token=ctx.token),
                fmt=format,
            )
        report_nav = build_report_nav(
            token=ctx.token, report_id=cached.report_id, title=cached.title,
            day_iso=cached.date.isoformat(),
            reports_in_sitting=index.reports_for_sitting(cached.date.isoformat()),
            terms=index.terms_by_norm(),
        )
        return HTMLResponse(
            render.render_report(
                report=cached, token=ctx.token, retrieved=_now_iso(),
                report_nav=report_nav,
            ),
            headers=_PROTECTED_HEADERS,
        )



