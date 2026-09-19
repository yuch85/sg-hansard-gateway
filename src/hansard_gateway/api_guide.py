"""Consumer-facing API guide + R-2b OpenAPI spec parameter overrides.

Split out of ``main.py`` to keep that module under the 300-LOC limit (STYLE.md
Rule 13). Two responsibilities, both contract/documentation only (no behavior):

1. ``API_GUIDE`` — the Markdown published as ``/openapi.json`` ``info.description``
   so an agent handed only the base URL + a token can self-orient.
2. ``apply_spec_overrides`` / ``patched_openapi`` — merge the ``format`` enum and
   ISO-date param shapes into the generated spec (the routes declare those
   params as plain ``str`` and validate at runtime; the spec declares the
   machine contract). See ``openapi_schemas.py`` for the JSON 200/response
   declarations attached via ``responses=`` on the routes.
"""

from __future__ import annotations

from typing import Callable

from fastapi import FastAPI

#: Consumer-facing API guide, published in /openapi.json (info.description) so
#: an agent handed ONLY the base URL + a token can self-orient.
API_GUIDE = """Read-only gateway to Singapore Parliamentary Reports (Hansard), served as
plain HTML/JSON/text. Every page is server-rendered: no JavaScript, no login
forms, no third-party resources.

## How to use it

1. You hold a capability token (a string like ``hg_...``). ALL content lives
   under the prefix ``/a/{token}/`` — substitute your token verbatim.
   Invalid or revoked tokens return HTTP 404 on every path (by design:
   identical body, no enumeration).
2. Start at ``GET /a/{token}/`` — it lists the endpoint families with
   token-preserving example links.
3. Search: ``GET /a/{token}/search?q=...`` returns a results page whose
   links are token-preserving. Follow one to read a report.
4. Read a report: ``GET /a/{token}/report/{report_id}`` returns the full
   verbatim transcript with a SHA-256 integrity footer.
5. Browse a sitting: ``GET /a/{token}/date/{yyyy-mm-dd}`` lists every
   report for one sitting date.

## Formats

Every content route (report/search/date) accepts ``?format=html`` (default),
``json`` (structured data, best for programmatic parsing), or ``text``
(machine-readable plain text). HTML pages contain no scripts and no external
URLs other than the SPRS provenance link in the report footer.

## Errors

404 = unknown/invalid/revoked token OR unknown report id. 422 = bad
parameters. 429 = your token's rate limit. 502/503 = the upstream parliament
site failed or was saturated; retry later — no content is fabricated.

## Retry semantics (429 / 502 / 503)

No ``Retry-After`` header is emitted today. On 429 (your rate limit), apply
bounded exponential backoff with jitter (start around 1-2s, double per
attempt, cap at roughly 30s) and stop after a few attempts — do not retry in
a tight loop. On 502/503 (upstream failure or saturation), the upstream is
transiently down; back off the same way and retry later. 404 and 422 are
terminal for that request — retrying the identical request will not help; fix
the token or the parameters first.

## Public (no token)

``/`` and ``/about`` (documentation), ``/health`` (liveness JSON),
``/robots.txt``, ``/openapi.json`` (this document), ``/docs`` (interactive
Swagger UI)."""


def apply_spec_overrides(spec: dict[str, object]) -> None:
    """Merge the R-2b OpenAPI parameter overrides into the generated spec.

    The generated ``format`` query param is a plain string (the route declares
    ``format: str`` and validates at runtime); the overrides replace its schema
    with the enum. The generated ``from_``/``to``/``day`` date params are bare
    strings; the overrides add ``format: date`` + a ``yyyy-mm-dd`` pattern.
    Idempotent: safe to run on every spec build (``app.openapi()`` is cached).
    """
    from hansard_gateway.openapi_schemas import (
        format_query_param,
        iso_date_path_param,
        iso_date_query_param,
    )

    overrides: dict[tuple[str, str], dict[str, object]] = {
        ("query", "format"): format_query_param(),
        ("query", "from_"): iso_date_query_param(
            "from_", "Earliest sitting date filter (yyyy-mm-dd)."
        ),
        ("query", "to"): iso_date_query_param(
            "to", "Latest sitting date filter (yyyy-mm-dd)."
        ),
        # R-3: ``day`` is a PATH variable — use the path builder so the
        # override does NOT clobber FastAPI's correct ``in: path`` /
        # ``required: true`` (the query builder emitted ``in: query`` and
        # ``param.update`` overwrote it).
        ("path", "day"): iso_date_path_param(
            "day", "The sitting date (yyyy-mm-dd)."
        ),
    }

    def _patch(params: object) -> None:
        if not isinstance(params, list):
            return
        for param in params:
            if not isinstance(param, dict):
                continue
            key = (param.get("in"), param.get("name"))
            if key in overrides:
                param.update(overrides[key])

    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for op in path_item.values():
            if isinstance(op, dict):
                _patch(op.get("parameters"))


def patched_openapi(app: FastAPI) -> Callable[[], dict[str, object]]:
    """Return a spec builder that applies the R-2b param overrides then caches.

    FastAPI caches the first ``app.openapi()`` result on ``app.openapi``. This
    wrapper decorates the original builder so the overrides are applied on the
    first (and only) build — subsequent calls hit the cache.
    """
    original: Callable[[], dict[str, object]] = app.openapi
    state: dict[str, dict[str, object]] = {}

    def _builder() -> dict[str, object]:
        if "spec" in state:
            return state["spec"]
        spec = original()
        apply_spec_overrides(spec)
        state["spec"] = spec
        return spec

    return _builder
