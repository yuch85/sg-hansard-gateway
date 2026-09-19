"""Centralized error responses: the format-aware error envelope (consumer R-2a).

Every error response honors the requested ``format`` (spec §12):

- ``html``   — the existing static Jinja2 error page (``text/html``).
- ``json``   — ``{"error": {"code", "message", "retryable"}}`` (application/json).
- ``text``   — one plain-text line (text/plain).

Status codes and their machine codes are fixed by named constants below;
``retryable`` is true for 429/502/503, false for 404/422 (CO R-2a contract).
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from hansard_gateway.config import settings
from hansard_gateway import render

#: Machine codes for the JSON/text error envelope (consumer R-2a).
CODE_NOT_FOUND: str = "not_found"
CODE_INVALID_PARAMETER: str = "invalid_parameter"
CODE_RATE_LIMITED: str = "rate_limited"
CODE_UPSTREAM_UNAVAILABLE: str = "upstream_unavailable"

#: Statuses whose envelope carries ``retryable: true``.
_RETRYABLE_STATUSES: frozenset[int] = frozenset(
    {settings.http_too_many_requests,
     settings.http_bad_gateway,
     settings.http_unavailable}
)

#: Human message per status (shared by the json and text envelopes).
_MESSAGES: dict[int, str] = {
    settings.http_not_found: "The requested resource was not found.",
    settings.http_unprocessable: "One or more request parameters are invalid.",
    settings.http_too_many_requests: "Rate limit exceeded.",
    settings.http_bad_gateway: "Upstream temporarily unavailable.",
    settings.http_unavailable: "Upstream saturated.",
    settings.http_gateway_timeout: "Upstream timed out.",
}

#: Response headers applied to every protected (token-bearing) error response.
_PROTECTED_HEADERS: dict[str, str] = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}

#: Media types (spec §12).
_JSON_MEDIA: str = "application/json"
_TEXT_MEDIA: str = "text/plain"


def _message_for(status: int) -> str:
    """Human-readable message for a status (no silent default)."""
    try:
        return _MESSAGES[status]
    except KeyError as exc:
        raise ValueError(f"no error message registered for status {status}") from exc


def build_error_response(
    *,
    status: int,
    code: str,
    fmt: str,
    token: Optional[str] = None,
    report_id: str = "",
    correction_url: Optional[str] = None,
    detail: Optional[str] = None,
) -> Response:
    """Build the correctly-typed error response for (status, fmt).

    ``html`` renders the static error page (report_id neutralized to "" by the
    caller for token failures, so the body is byte-identical across paths —
    addendum §7 anti-enumeration). ``json`` emits the envelope; ``text`` a
    single line. ``token`` non-None adds the protected no-store headers and
    renders the nav-bar branch. ``correction_url`` (422-with-navigation,
    spec §6.3) appends a finished link to the corrected form; ``detail`` is
    the plain-language 'what was wrong' line (spec §6.3) — both only for
    VALID-token errors, so the no-enumeration 404 path (token=None) is
    untouched.
    """
    headers = _PROTECTED_HEADERS if token is not None else {}
    if fmt == "html":
        return HTMLResponse(
            render.render_error(
                report_id=report_id, token=token,
                correction_url=correction_url, detail=detail,
                status=status,
            ),
            status_code=status,
            headers=headers,
        )
    if fmt == "json":
        body = json.dumps(
            {"error": {"code": code,
                       "message": _message_for(status),
                       "retryable": status in _RETRYABLE_STATUSES}},
            indent=2,
        )
        return Response(
            content=body, status_code=status,
            media_type=_JSON_MEDIA, headers=headers,
        )
    return PlainTextResponse(
        f"{_message_for(status)} ({code})", status_code=status, headers=headers,
    )
