"""FastAPI app factory: public + protected routers, auth/rate/cache wiring.

Protected surface (Plan 04 complete): ``/a/{token}/report/{id}`` (tracer),
plus ``/search`` and ``/date/{d}`` (registered by search_routes.py) — each
with JSON/text format variants (spec §12). Public routes + authenticated home.

Security (addendum §11/§12/§13): every protected response carries
``Cache-Control: private, no-store``, ``Referrer-Policy: no-referrer``, and
``X-Robots-Tag: noindex, nofollow, noarchive``. Structured logging records the
token *label* only (D-09/D-10, release-blocking); uvicorn access logging is off.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from hansard_gateway.auth import (
    AuthContext,
    TokenRejected,
    require_capability_token,
)
from hansard_gateway.api_guide import API_GUIDE, patched_openapi
from hansard_gateway.config import Settings, settings
from hansard_gateway import render
from hansard_gateway import error_responses
from hansard_gateway.logging_setup import (
    RequestLoggerMiddleware,
    run,  # noqa: F401  (uvicorn entrypoint with access_log=False, D-10)
)
from hansard_gateway.index.loader import IndexService
from hansard_gateway.facet_routes import register_facet_routes
from hansard_gateway.nav_routes import register_nav_routes
from hansard_gateway.rate_limit import RateGate
from hansard_gateway.report_handler import register_report_route
from hansard_gateway.search_routes import register_search_routes
from hansard_gateway.validation_errors import validation_error_response
from hansard_gateway.sprs.cache import ReportCache
from hansard_gateway.sprs.client import UpstreamError

logger = logging.getLogger(__name__)

#: HTTP status for a rejected capability token (addendum §7 — 404, not 401).
_TOKEN_REJECTED_STATUS = settings.http_not_found

#: robots.txt body (addendum §13) — keeps token-bearing URLs out of indexes.
#: 2026-09-18 (Claude-User compat): Anthropic's USER-DIRECTED retrieval client
#: (Claude-User — "requests where a Claude user asks it to retrieve web
#: content") honours robots.txt and was refusing the whole site under the
#: blanket Disallow; explicit per-agent rules now ALLOW the Anthropic
#: user-facing agents — Claude-User (user-directed retrieval) and
#: Claude-SearchBot (Claude web search) — get explicit Allow groups,
#: and the WILDCARD group allows /a/ too: Claude's fetcher does not
#: expose its UA (its own diagnosis, 2026-09-18), so an unknown product
#: token would fall through to `*` — under RFC 9309 the wildcard governs
#: when no specific group matches, so only `*`'s Allow makes the policy
#: robust. ClaudeBot is allowed the SAME way: /a/ is token-gated and
#: noindex'd (see below), so nothing indexable or unauthenticated exists
#: there for any crawler. Non-/a/ paths stay Disallowed for all. This is
#: retrieval policy, NOT access control (RFC 9309): the token remains
#: the only gate, and a crawler without one gets the identical 404.
#: X-Robots-Tag noindex on every protected page keeps indexing blocked
_ROBOTS_BODY = (
    "User-agent: Claude-User\n"
    "Allow: /a/\n"
    "\n"
    "User-agent: Claude-SearchBot\n"
    "Allow: /a/\n"
    "\n"
    "User-agent: ClaudeBot\n"
    "Allow: /a/\n"
    "\n"
    "User-agent: *\n"
    "Allow: /a/\n"
    "Disallow: /\n"
)

#: Response headers applied to every protected (token-bearing) response.
_PROTECTED_HEADERS: dict[str, str] = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}

#: The ISO timestamp format for the provenance "Retrieved" line (spec §13).
_RETRIEVED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Allowed ``format`` query values (spec §12); HTML is the default.
_FORMATS: frozenset[str] = frozenset({"html", "json", "text"})


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 Zulu string."""
    return datetime.now(timezone.utc).strftime(_RETRIEVED_FORMAT)


def _token_rejected_body(request: Request) -> Response:
    """Identical 404 for any token failure (no enumeration, addendum §7).

    The report_id is NOT echoed: the body is byte-identical across every path
    and token (consumer R-2a anti-enumeration fix). The ``format`` query
    param is honored; the envelope carries the ``not_found`` code.
    """
    fmt = request.query_params.get("format", "html")
    if fmt not in _FORMATS:
        fmt = "html"
    return error_responses.build_error_response(
        status=_TOKEN_REJECTED_STATUS,
        code=error_responses.CODE_NOT_FOUND,
        fmt=fmt,
        token=None,
        report_id="",
    )


def _upstream_status(exc: UpstreamError) -> int:
    """Map an UpstreamError to the spec §17 status contract (502/503/504)."""
    status = exc.status
    if status in (settings.http_bad_gateway, settings.http_unavailable, settings.http_gateway_timeout):
        return status
    return settings.http_bad_gateway


def _format_response(body: str, *, fmt: str) -> Response:
    """Wrap a serialized body with the correct media type (spec §12)."""
    if fmt == "json":
        return Response(content=body, media_type="application/json", headers=_PROTECTED_HEADERS)
    return PlainTextResponse(body, headers=_PROTECTED_HEADERS)


def _error_response(
    report_id: str, token: Optional[str], exc: UpstreamError,
    *, fmt: str = "html",
) -> Response:
    """Upstream-failure response with the spec §17 status (no fabrication).

    ``html`` renders the static error page (report_id may be echoed); ``json``/
    ``text`` emit the R-2a error envelope. ``retryable`` is derived from the
    status (502/503/504 → true).
    """
    status = _upstream_status(exc)
    return error_responses.build_error_response(
        status=status,
        code=error_responses.CODE_UPSTREAM_UNAVAILABLE,
        fmt=fmt,
        token=token,
        report_id=report_id,
    )


def create_app(
    *,
    app_settings: Optional[Settings] = None,
    index_override: Optional[IndexService] = None,
) -> FastAPI:
    """Build the FastAPI application with all routers and dependencies wired.

    ``app_settings`` is an optional override; the module-level ``settings``
    singleton is the default. ``index_override`` injects a pre-built
    :class:`IndexService` (tests); by default one is opened against
    ``cfg.index_db_path`` — which degrades to an empty state if the file is
    absent, so app boot never depends on the index (live-retrieval invariant).
    """
    cfg = app_settings if app_settings is not None else settings

    app = FastAPI(title="Singapore Hansard Gateway", description=API_GUIDE)

    # Shared, stateful dependencies (in-process; single uvicorn worker).
    gate = RateGate(settings=cfg)
    cache = ReportCache(settings=cfg)
    index = index_override or IndexService(path=cfg.index_db_path, settings=cfg)

    app.state.gate = gate
    app.state.cache = cache
    app.state.index = index

    @app.exception_handler(TokenRejected)
    async def _reject(request: Request, exc: TokenRejected) -> Response:
        return _token_rejected_body(request)

    @app.exception_handler(RequestValidationError)
    async def _validation_rejected(
        request: Request, exc: RequestValidationError
    ) -> Response:
        """422 for FastAPI type validation (spec §6.3) — delegates to
        :mod:`validation_errors` (300-LOC split)."""
        return validation_error_response(
            request, exc, token_rejected_body=_token_rejected_body)

    # --- Public router (no prefix, no auth) ---
    public = APIRouter()

    @public.get("/", response_class=HTMLResponse)
    async def public_home() -> str:
        """Public home: documentation with placeholder links only."""
        return render.render_index()

    @public.get("/about", response_class=HTMLResponse)
    async def about() -> str:
        """Public about: provenance / not-affiliated statement (spec §25)."""
        return render.render_about()

    @public.get("/health")
    async def health() -> dict[str, str]:
        """Local health: JSON status with NO upstream call (spec §24)."""
        return {"status": "ok"}

    @public.get("/robots.txt", response_class=PlainTextResponse)
    async def robots() -> str:
        """robots.txt: disallow the token-bearing prefix (addendum §13).

        Serves the operator's override file when ``cfg.robots_path`` points at
        an existing file (27.2-REQ-05); otherwise the built-in default body.
        Retrieval policy, NOT access control (RFC 9309). Closes over ``cfg``
        (the per-app settings) so the ``app_settings`` test seam works.
        """
        override = cfg.robots_path
        if override is not None and override.exists():
            return override.read_text(encoding="utf-8")
        return _ROBOTS_BODY

    app.include_router(public)

    # --- Protected router (prefix carries the capability token) ---
    protected = APIRouter(
        prefix="/a/{token}",
        dependencies=[Depends(require_capability_token)],
    )


    # Report route (extracted for the 300-LOC rule; wave 4 404 split).
    register_report_route(protected, cfg=cfg, gate=gate, cache=cache, index=index)

    # Search + date TOC routes (Plan 04) — same protected router, same gate/cache.
    register_search_routes(
        protected, cfg=cfg, gate=gate, cache=cache, index=index
    )
    # Launcher + prefix ladder + facet routes (Phase 27.1 wave 2) — index-only,
    # quota-exempt, same protected router (token gate at router level).
    register_nav_routes(protected, cfg=cfg, index=index)
    register_facet_routes(protected, index=index)

    app.include_router(protected)

    # Structured request logging: one sanitized JSON line per authenticated
    # request (token_label, never the token). Added last so it wraps all routes.
    app.add_middleware(RequestLoggerMiddleware, settings=cfg)

    # Apply the R-2b machine contract (enum date params) to the cached spec.
    app.openapi = patched_openapi(app)
    return app


#: Module-level app for `uvicorn hansard_gateway.main:app`.
app = create_app()
