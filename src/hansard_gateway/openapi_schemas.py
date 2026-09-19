"""Machine-readable OpenAPI contract for the protected content routes (R-2b).

The three content routes (``/search``, ``/date/{day}``, ``/report/{report_id}``)
return ``Response`` objects built directly from the ``format`` query param
(spec §12), so FastAPI's ``response_model`` cannot express the per-format media
types. This module therefore builds plain-dict JSON Schemas (draft-2020-12)
and attaches them to each route via the ``responses`` mapping.

This is a CONTRACT-ONLY declaration: the JSON emitted at runtime is fixed by
``models.py`` + ``render/__init__.py`` and already covered by the test suite.
Nothing here changes behavior — it only makes the machine contract complete
so a programmatic consumer handed ``/openapi.json`` + a token can know the
result arrays, the ``report_id``/``link_id`` fields, the followable link form,
pagination, the transcript location, the SHA-256 field, and the R-2a error
envelope on the 422/429/502/503 responses.
"""

from __future__ import annotations

from typing import Any

#: Media types per ``format`` value (spec §12).
JSON_MEDIA = "application/json"
HTML_MEDIA = "text/html"
TEXT_MEDIA = "text/plain"

#: The ``format`` enum values, in canonical order (spec §12).
FORMAT_ENUM: tuple[str, ...] = ("html", "json", "text")

#: ISO calendar-date pattern enforced by ``_parse_iso_date`` (search_routes).
ISO_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

#: ISO calendar-date type (used for ``format``/``pattern`` on date params).
ISO_DATE_TYPE = "date"

#: The token-preserving follow-link path template for a search hit.
LINK_PATH_TEMPLATE = "/a/{token}/report/{link_id}"

#: Description: a hit's ``link_id`` resolves via the token-preserving link.
LINK_DESCRIPTION = (
    "Report identifier. To follow this hit, GET the token-preserving link "
    f"``{LINK_PATH_TEMPLATE}`` — substitute your token for ``{{token}}`` and "
    "this value for ``{{link_id}}``. (The ``format=text`` rendering prints "
    "the bare ``/report/{link_id}``; the token-bearing form above is the "
    "canonical one to fetch.)"
)

#: 404 body is an HTML error page, identical for invalid/revoked token OR
#: unknown/invalid report id (anti-enumeration, addendum §7).
NOT_FOUND_DESCRIPTION = (
    "Invalid/revoked token OR unknown/invalid report id — the SAME body for "
    "both (identical HTML error page, by design: no enumeration)."
)

#: 422 body is a JSON error envelope (consumer R-2a; error_responses.py).
UNPROCESSABLE_DESCRIPTION = (
    "Bad parameters (query too long, date not yyyy-mm-dd, bad format, invalid "
    "report id). JSON: the error envelope "
    "``{\"error\": {\"code\": \"invalid_parameter\", \"message\": <string>, "
    "\"retryable\": false}}``."
)

#: 429 body is a JSON error envelope (consumer R-2a; error_responses.py).
TOO_MANY_DESCRIPTION = (
    "Rate limit exhausted. JSON: ``{\"error\": {\"code\": \"rate_limited\", "
    "\"message\": \"Rate limit exceeded.\", \"retryable\": true}}``. No "
    "Retry-After; apply bounded exponential backoff and retry."
)

#: 502/503: error envelope for json, one-line text for text, HTML page for html.
UPSTREAM_DESCRIPTION = (
    "Upstream (SPRS) temporarily unavailable (502) or saturated (503). "
    "JSON: ``{\"error\": {\"code\": \"upstream_unavailable\", \"message\": "
    "<string>, \"retryable\": true}}``; text: a one-line message; html: the "
    "static error page. Retry with bounded exponential backoff; no content "
    "is fabricated."
)

#: R-3: canonicalization for ``transcript_sha256`` so an independent consumer
#: can recompute the digest from the JSON. Must stay in sync with the actual
#: algorithm (``sprs/current.py::_transcript_sha256`` and the byte-identical
#: ``sprs/legacy.py::_transcript_sha256``).
TRANSCRIPT_SHA256_DESCRIPTION = (
    "SHA-256 hex digest of the normalized transcript (integrity). "
    "Canonicalization (R-3): flatten ``speeches`` in ``sequence`` order and "
    "collect every ``paragraph`` string verbatim (no speaker labels, no "
    "per-speech separator); join the paragraphs with a single line feed "
    "(\"\\n\"); UTF-8-encode; SHA-256; hex-encode lowercase. Speaker fields "
    "are NOT part of the digest. The HTML and JSON renderings of the same "
    "report expose the identical digest."
)


def _error_envelope_schema() -> dict[str, Any]:
    """The R-2a JSON error envelope schema (code/message/retryable)."""
    return {
        "type": "object",
        "properties": {
            "error": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"}, "message": {"type": "string"},
                    "retryable": {"type": "boolean"},
                },
                "required": ["code", "message", "retryable"],
            }
        },
        "required": ["error"],
    }


def _optional_str(description: str) -> dict[str, Any]:
    """A nullable string property (absent values serialize to ``null``)."""
    return {"type": ["string", "null"], "description": description}


def _search_hit_schema() -> dict[str, Any]:
    """Schema for one ``SearchHit`` (``asdict`` shape, date → ISO string)."""
    return {
        "type": "object",
        "properties": {
            "report_id": {"type": "string",
                          "description": "Spec-style report id, e.g. ``037_20041019_S0004_T0023``."},
            "link_id": {"type": "string", "description": LINK_DESCRIPTION},
            "date": {"type": ["string", "null"], "format": ISO_DATE_TYPE,
                     "description": "Sitting date (yyyy-mm-dd) or null."},
            "title": {"type": "string"},
            "report_type": _optional_str("Section/topic type, e.g. 'Bill' or null."),
            "speaker": _optional_str("Primary speaker (member name) or null."),
            "excerpt": _optional_str("Result excerpt snippet or null."),
            "url": {"type": "string",
                    "description": ("Absolute, token-bearing URL for this hit's full report — "
                                    "GET it directly to read the transcript (no need to construct "
                                    "the URL). Server-generated from your request token.")},
        },
    }


def _speech_schema() -> dict[str, Any]:
    """Schema for one ``Speech`` (``asdict`` shape)."""
    return {
        "type": "object",
        "properties": {
            "sequence": {"type": "integer", "description": "1-based speech order in the sitting."},
            "speaker_original": _optional_str("Source speaker text verbatim (legal record)."),
            "speaker_name": _optional_str("Normalized member name or null."),
            "speaker_role": _optional_str("Normalized role (e.g. 'MP', 'Minister') or null."),
            "paragraphs": {"type": "array", "items": {"type": "string"},
                           "description": "Verbatim transcript paragraphs, in order."},
        },
    }


def _search_page_schema() -> dict[str, Any]:
    """Schema for the ``/search`` JSON body (``asdict(SearchPage)``)."""
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search phrase echoed back."},
            "total": {"type": "integer", "description": "Total matching hits (all pages)."},
            "page": {"type": "integer", "description": "Current page (1-based)."},
            "limit": {"type": "integer", "description": "Hits per page."},
            "hits": {"type": "array", "items": _search_hit_schema()},
            "provider": {"type": "string", "description": "Authoritative provider ('sprs')."},
            "rendered_total": {"type": "integer",
                               "description": "Exact number of hits the provider "
                                              "retrieved for this query (F-4); "
                                              "'total' is an upstream estimate."},
        },
    }


def _date_toc_schema() -> dict[str, Any]:
    """Schema for the ``/date/{day}`` JSON body (``render_date_format``)."""
    return {
        "type": "object",
        "properties": {
            "date": {"type": "string", "format": ISO_DATE_TYPE,
                     "description": "The sitting date (yyyy-mm-dd), echoed back."},
            "hits": {"type": "array", "items": _search_hit_schema(),
                     "description": "Every report for that sitting (TOC)."},
        },
    }


def _report_schema() -> dict[str, Any]:
    """Schema for the ``/report/{report_id}`` JSON body (``asdict(HansardReport)``)."""
    return {
        "type": "object",
        "properties": {
            "report_id": {"type": "string",
                          "description": "The report id that was requested (spec-style or live reportId)."},
            "date": {"type": "string", "format": ISO_DATE_TYPE,
                     "description": "Sitting date (yyyy-mm-dd)."},
            "title": {"type": "string"},
            "topic_type": _optional_str("Topic/section type or null."),
            "source_url": {"type": "string",
                           "description": "Official SPRS provenance URL."},
            "volume": _optional_str("Hansard volume or null."),
            "parliament_no": _optional_str("Parliament number or null."),
            "session_no": _optional_str("Session number or null."),
            "sitting_no": _optional_str("Sitting number or null."),
            "speeches": {"type": "array", "items": _speech_schema(),
                         "description": "The verbatim transcript, speaker by speaker."},
            "transcript_sha256": {"type": "string",
                                  "description": TRANSCRIPT_SHA256_DESCRIPTION},
            "url": {"type": "string",
                    "description": "Absolute, token-bearing URL for this report (self link)."},
            "links": {"type": "object",
                      "description": "Absolute, token-bearing navigation links (server-generated).",
                      "properties": {
                          "self": {"type": "string", "description": "URL of this report."},
                          "sitting": {"type": "string",
                                      "description": "URL of this sitting's table of contents (/date)."},
                      }},
        },
    }


def _text_body_schema(description: str) -> dict[str, Any]:
    """A ``text/plain`` body (content is a string; shape is prose)."""
    return {"content": {TEXT_MEDIA: {"schema": {"type": "string"}, "description": description}}}


def _html_body_schema(description: str) -> dict[str, Any]:
    """A ``text/html`` body (content is a string; shape is rendered HTML)."""
    return {"content": {HTML_MEDIA: {"schema": {"type": "string"}, "description": description}}}


def _json_body_schema(schema: dict[str, Any], description: str) -> dict[str, Any]:
    """An ``application/json`` body with the given named-object schema."""
    return {"content": {JSON_MEDIA: {"schema": schema, "description": description}}}


def search_responses() -> dict[str, Any]:
    """Full ``responses`` mapping for the ``/search`` operation."""
    return {
        "200": {
            "description": "A page of search results, in the requested format.",
            "content": {
                JSON_MEDIA: {"schema": _search_page_schema(),
                             "description": "Structured results page (format=json)."},
                HTML_MEDIA: {"schema": {"type": "string"},
                             "description": "Server-rendered results page (format=html, default)."},
                TEXT_MEDIA: {"schema": {"type": "string"},
                             "description": "Machine-readable plain text (format=text)."},
            },
        },
        "404": {"description": NOT_FOUND_DESCRIPTION, **_html_body_schema("HTML error page.")},
        "422": {"description": UNPROCESSABLE_DESCRIPTION,
                **_json_body_schema(_error_envelope_schema(), "Bad parameters.")},
        "429": {"description": TOO_MANY_DESCRIPTION,
                **_json_body_schema(_error_envelope_schema(), "Rate limited.")},
        "502": {"description": UPSTREAM_DESCRIPTION, **_json_body_schema(_error_envelope_schema(), "Upstream unavailable (json).")},
        "503": {"description": UPSTREAM_DESCRIPTION, **_json_body_schema(_error_envelope_schema(), "Upstream saturated (json).")},
    }


def date_responses() -> dict[str, Any]:
    """Full ``responses`` mapping for the ``/date/{day}`` operation."""
    return {
        "200": {
            "description": "The table of contents for one sitting date, in the requested format.",
            "content": {
                JSON_MEDIA: {"schema": _date_toc_schema(),
                             "description": "Sitting TOC (format=json)."},
                HTML_MEDIA: {"schema": {"type": "string"},
                             "description": "Server-rendered TOC (format=html, default)."},
                TEXT_MEDIA: {"schema": {"type": "string"},
                             "description": "Machine-readable plain text (format=text)."},
            },
        },
        "404": {"description": NOT_FOUND_DESCRIPTION, **_html_body_schema("HTML error page.")},
        "422": {"description": UNPROCESSABLE_DESCRIPTION,
                **_json_body_schema(_error_envelope_schema(), "Bad parameters.")},
        "429": {"description": TOO_MANY_DESCRIPTION,
                **_json_body_schema(_error_envelope_schema(), "Rate limited.")},
        "502": {"description": UPSTREAM_DESCRIPTION,
                **_json_body_schema(_error_envelope_schema(), "Upstream unavailable (json).")},
        "503": {"description": UPSTREAM_DESCRIPTION,
                **_json_body_schema(_error_envelope_schema(), "Upstream saturated (json).")},
    }


def report_responses() -> dict[str, Any]:
    """Full ``responses`` mapping for the ``/report/{report_id}`` operation."""
    return {
        "200": {
            "description": "One full Hansard report (verbatim transcript), in the requested format.",
            "content": {
                JSON_MEDIA: {"schema": _report_schema(),
                             "description": "Structured report with transcript + SHA-256 (format=json)."},
                HTML_MEDIA: {"schema": {"type": "string"},
                             "description": "Server-rendered report page (format=html, default)."},
                TEXT_MEDIA: {"schema": {"type": "string"},
                             "description": "Machine-readable plain text (format=text)."},
            },
        },
        "404": {"description": NOT_FOUND_DESCRIPTION, **_html_body_schema("HTML error page.")},
        "422": {"description": UNPROCESSABLE_DESCRIPTION,
                **_json_body_schema(_error_envelope_schema(), "Invalid report id / format.")},
        "429": {"description": TOO_MANY_DESCRIPTION,
                **_json_body_schema(_error_envelope_schema(), "Rate limited.")},
        "502": {"description": UPSTREAM_DESCRIPTION,
                **_json_body_schema(_error_envelope_schema(), "Upstream unavailable (json).")},
        "503": {"description": UPSTREAM_DESCRIPTION,
                **_json_body_schema(_error_envelope_schema(), "Upstream saturated (json).")},
    }


def format_query_param() -> dict[str, Any]:
    """The ``format`` query parameter schema (enum) shared by all three routes."""
    return {
        "name": "format",
        "in": "query",
        "description": "Response format: ``html`` (default), ``json`` (structured), or ``text`` (plain).",
        "required": False,
        "schema": {"type": "string", "enum": list(FORMAT_ENUM), "default": "html"},
    }


def iso_date_query_param(name: str, description: str) -> dict[str, Any]:
    """An optional ``yyyy-mm-dd`` query parameter (``from_`` / ``to``)."""
    return {
        "name": name,
        "in": "query",
        "description": description,
        "required": False,
        "schema": {"type": "string", "format": ISO_DATE_TYPE,
                   "pattern": ISO_DATE_PATTERN},
    }


def iso_date_path_param(name: str, description: str) -> dict[str, Any]:
    """A required ``yyyy-mm-dd`` PATH parameter (``/date/{day}``).

    R-3: distinct from :func:`iso_date_query_param` because ``day`` is a path
    variable — a generated client must substitute it into the URL, not a query
    string. The R-2b override previously reused the query builder here, and
    ``param.update`` clobbered FastAPI's correct ``in: path`` / ``required:
    true`` with ``in: query`` / ``required: false`` (consumer retest defect).
    """
    return {
        "name": name,
        "in": "path",
        "description": description,
        "required": True,
        "schema": {"type": "string", "format": ISO_DATE_TYPE,
                   "pattern": ISO_DATE_PATTERN},
    }
