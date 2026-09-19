"""FastAPI type-validation 422 handler (spec §6.3, Phase 27.1 wave 4).

Extracted from main.py for the 300-LOC rule. The handler converts a
RequestValidationError (e.g. limit=abc) into the same format-aware,
nav-bar-carrying error page the route handlers build for their own 422s.
The token is resolved from the REQUEST path against the token store
(T-27-33: never a route-name literal); unknown/invalid tokens fall to the
byte-identical tokenless body (anti-enumeration preserved).
"""

from __future__ import annotations

import hashlib
from typing import Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError

from hansard_gateway import error_responses
from hansard_gateway.auth import get_token_store
from hansard_gateway.config import settings
from hansard_gateway.render.recovery_links import corrected_form_for_422

#: Allowed ``format`` query values (spec §12); HTML is the default.
_FORMATS: frozenset[str] = frozenset({"html", "json", "text"})


def _resolve_token(path: str) -> Optional[str]:
    """The request's token when it is a stored ENABLED token, else None."""
    if not path.startswith("/a/"):
        return None
    segments = path.split("/")
    if len(segments) < 3:
        return None
    candidate = segments[2]
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    for entry in get_token_store().enabled_entries():
        if entry.sha256 == digest:
            return candidate
    return None


def _validation_route_for(path: str) -> str:
    """The corrected-form route name for a type-validation 422 path ('' when
    the path carries no inferable correction)."""
    if "/search" in path:
        return "search"
    if "/date/" in path:
        return "date"
    return ""


def validation_error_response(
    request: Request,
    exc: RequestValidationError,
    token_rejected_body,
) -> "Response":
    """The 422 for a FastAPI type-validation failure (spec §6.3).

    ``token_rejected_body`` is a zero-arg callable returning the
    byte-identical tokenless 404 body (the anti-enumeration fallback)."""
    path = request.url.path
    fmt = request.query_params.get("format", "html")
    if fmt not in _FORMATS:
        fmt = "html"
    token = _resolve_token(path)
    if token is None:
        return token_rejected_body(request)
    first = exc.errors()[0] if exc.errors() else {}
    loc = first.get("loc", ())
    param = str(loc[2]) if len(loc) > 2 else ""
    value = str(first.get("input", ""))
    correction = detail = None
    route = _validation_route_for(path)
    if route:
        correction, detail = corrected_form_for_422(
            token=token, route=route, param=param, value=value,
            query=request.query_params.get("q", ""),
            date_from=request.query_params.get("from_", "") or "",
            date_to=request.query_params.get("to", "") or "",
            speaker=request.query_params.get("speaker", "") or "",
        )
    return error_responses.build_error_response(
        status=settings.http_unprocessable,
        code=error_responses.CODE_INVALID_PARAMETER,
        fmt=fmt, token=token,
        correction_url=correction, detail=detail)
