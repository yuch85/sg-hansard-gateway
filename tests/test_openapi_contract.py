"""R-2b: pin the machine-readable OpenAPI contract (consumer rectify).

These tests assert the three content ops expose a complete machine contract:
non-empty named JSON 200 schemas, per-format media types, the ``format`` enum,
ISO-date patterns, and the declared error responses (404/422/429/502/503).

Offline — ``app.openapi()`` is built in-process; no upstream, no token needed.
"""

from __future__ import annotations

from typing import Any

from hansard_gateway.main import create_app


def _spec() -> dict[str, Any]:
    """Build the OpenAPI spec once per test module (in-process, cached)."""
    return create_app().openapi()


def _op(path: str, method: str = "get") -> dict[str, Any]:
    """Return the operation object for a path+method in the spec."""
    return _spec()["paths"][path][method]


def _param(op: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the named parameter object (raises if absent)."""
    for p in op.get("parameters", []):
        if p.get("name") == name:
            return p
    raise AssertionError(f"parameter {name!r} not found")


SEARCH = "/a/{token}/search"
DATE = "/a/{token}/date/{day}"
REPORT = "/a/{token}/report/{report_id}"


def test_search_200_json_schema_named() -> None:
    """/search 200 JSON has the SearchPage named properties."""
    props = _op(SEARCH)["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert set(props) == {"query", "total", "page", "limit", "hits", "provider",
                          "rendered_total"}


def test_search_200_media_types() -> None:
    """/search 200 declares all three format media types."""
    media = _op(SEARCH)["responses"]["200"]["content"]
    assert set(media) == {"application/json", "text/html", "text/plain"}


def test_date_200_json_schema_named() -> None:
    """/date 200 JSON has the TOC named properties."""
    props = _op(DATE)["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert set(props) == {"date", "hits"}


def test_report_200_json_schema_named() -> None:
    """/report 200 JSON has the HansardReport named properties incl SHA-256."""
    props = _op(REPORT)["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    expected = {
        "report_id", "date", "title", "topic_type", "source_url", "volume",
        "parliament_no", "session_no", "sitting_no", "speeches", "transcript_sha256",
        "url", "links",
    }
    assert set(props) == expected
    assert "self" in props["links"]["properties"]
    assert "sitting" in props["links"]["properties"]


def test_format_enum_on_all_three_ops() -> None:
    """Every content op exposes the format query param with the html/json/text enum."""
    for path in (SEARCH, DATE, REPORT):
        fmt = _param(_op(path), "format")
        assert fmt["schema"].get("enum") == ["html", "json", "text"], path


def test_date_params_iso_pattern() -> None:
    """from_/to (query) and day (path) carry format=date + yyyy-mm-dd pattern."""
    for name in ("from_", "to"):
        schema = _param(_op(SEARCH), name)["schema"]
        assert schema.get("format") == "date", name
        assert schema.get("pattern") == r"^\d{4}-\d{2}-\d{2}$", name
    day = _param(_op(DATE), "day")
    assert day["schema"].get("format") == "date"
    assert day["schema"].get("pattern") == r"^\d{4}-\d{2}-\d{2}$"


def test_day_param_is_path_required() -> None:
    """R-3: ``day`` is a required PATH param (the R-2b override clobbered it
    to in:query/required:false — a generated client would build the wrong URL)."""
    day = _param(_op(DATE), "day")
    assert day["in"] == "path", f"day must be a path param, got in={day.get('in')!r}"
    assert day["required"] is True, f"day must be required, got {day.get('required')!r}"


def test_transcript_sha256_canonicalization_documented() -> None:
    """R-3: the transcript_sha256 schema documents its canonicalization so an
    independent consumer can recompute the digest (paragraphs joined with \\n,
    UTF-8, SHA-256; speakers excluded)."""
    desc = (
        _op(REPORT)["responses"]["200"]["content"]["application/json"]
        ["schema"]["properties"]["transcript_sha256"]["description"]
    )
    assert "SHA-256" in desc
    assert "sequence" in desc            # flatten in sequence order
    assert "line feed" in desc or "\\n" in desc  # join with a single \n
    assert "UTF-8" in desc
    assert "NOT part of the digest" in desc  # speakers excluded


def test_error_responses_declared_on_all_three_ops() -> None:
    """404/422/429/502/503 are declared on every content op."""
    expected = {"404", "422", "429", "502", "503"}
    for path in (SEARCH, DATE, REPORT):
        declared = set(_op(path)["responses"])
        assert expected <= declared, f"{path}: missing {expected - declared}"


def test_error_envelope_shape_on_all_content_ops() -> None:
    """R-2a: 422/429/502/503 declare the {error:{code,message,retryable}} envelope."""
    envelope = {
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
    for path in (SEARCH, DATE, REPORT):
        for status in ("422", "429", "502", "503"):
            schema = _op(path)["responses"][status]["content"]["application/json"]["schema"]
            assert schema == envelope, f"{path} {status}: envelope mismatch"


def test_429_envelope_documented_and_404_anti_enum() -> None:
    """429 prose pins rate_limited/retryable; 404 documents identical-body design."""
    too_many = _op(SEARCH)["responses"]["429"]["description"]
    assert "rate_limited" in too_many and '"retryable": true' in too_many
    nf = _op(REPORT)["responses"]["404"]["description"]
    assert "SAME body" in nf and "enumeration" in nf


def test_search_hit_link_documented() -> None:
    """The follow-link form (token-preserving) is documented on the hit's link_id."""
    hit = (
        _op(SEARCH)["responses"]["200"]["content"]["application/json"]
        ["schema"]["properties"]["hits"]["items"]
    )
    desc = hit["properties"]["link_id"]["description"]
    assert "/a/{token}/report/{link_id}" in desc
    assert "url" in hit["properties"], "self-navigating absolute url must be declared on the hit"


def test_search_hit_url_absolute() -> None:
    """Self-URL change: each hit declares an absolute, token-bearing url field."""
    hit = (
        _op(SEARCH)["responses"]["200"]["content"]["application/json"]
        ["schema"]["properties"]["hits"]["items"]["properties"]
    )
    assert hit["url"]["type"] == "string"
    assert "token" in hit["url"]["description"]
