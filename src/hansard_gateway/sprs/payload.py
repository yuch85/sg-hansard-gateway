"""Shared topic-payload view + parser dispatch over one upstream (D-01).

``SprsClient.fetch_topic`` returns the raw ``resultHTML`` dict. The two eras
carry the transcript in different fields (sprs2 ``htmlContent`` full document /
sprs3 ``content`` fragment) and metadata in different places (sprs2 ``<meta>``
tags / sprs3 top-level fields). ``TopicPayload`` normalises that into one view
the parsers consume, and ``parse_topic`` branches on ``reportVersion``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from hansard_gateway.models import HansardReport

#: reportVersion values (D-01) — the two parser branches.
SPRS2 = "sprs2"
SPRS3 = "sprs3"

#: sprs2 payloads may omit ``reportVersion``; the htmlContent full-document
#: shape identifies them.
_HTML_CONTENT_FIELD = "htmlContent"
_CONTENT_FIELD = "content"

#: The ``resultHTML`` field carrying the raw transcript for each era.
_REPORT_VERSION_FIELD = "reportVersion"


@dataclass(frozen=True)
class TopicPayload:
    """One fetched topic normalised for the parsers (era-agnostic view).

    ``html_content`` is the sprs2 full document (``htmlContent``); ``content``
    is the sprs3 fragment (``content``). The unused era field is ``None``.
    """

    report_version: Optional[str]
    html_content: Optional[str]
    content: Optional[str]
    report_id: str
    title: Optional[str]
    sitting_date: Optional[str]
    report_type: Optional[str]
    volume_no: Optional[str]
    parl_no: Optional[str]
    session_no: Optional[str]
    sitting_no: Optional[str]
    source_url: str


def _opt_str(value: Any) -> Optional[str]:
    """Coerce a metadata field to a stripped non-empty string, else None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


#: SPRS is an Angular SPA: its only real page is ``/search/``. A sitting's
#: official record IS addressable, though — the app's own "show full report"
#: flow opens the pre-2013 silo at ``#/report?sittingdate=<D-M-YYYY>`` and the
#: post-2012 silo at ``#/fullreport?sittingdate=<D-M-YYYY>`` (verified live
#: 2026-09-19: both routes render the full sitting transcript). There is NO
#: public per-REPORT (topic) URL — a topic is reachable only via an in-app
#: search. ``/hansard/<id>`` (the former scheme) 404s.
_SPRS2_REPORT_ROUTE = "/search/#/report"
_SPRS3_REPORT_ROUTE = "/search/#/fullreport"
#: Sitting dates strictly after this one live in the sprs3 silo (SPRS SPA
#: boundary, 2012-09-10).
_SPRS3_BOUNDARY_ISO = "2012-09-10"


def topic_source_url(*, report_id: str, sitting_date_iso: str) -> str:
    """Official public SPRS sitting-record URL for provenance (spec §13).

    Points at the SPA sitting route for the sitting the report belongs to
    (pre-2013 ``#/report``, post-2012 ``#/fullreport``). Uses the public base
    (config), NOT the ``/search`` API base, and carries no capability token.
    ``sitting_date_iso`` must be the report's sitting date as ISO ``YYYY-MM-DD``.
    """
    from datetime import date as _date

    from hansard_gateway.config import settings as _settings

    d = _date.fromisoformat(sitting_date_iso)
    route = (
        _SPRS3_REPORT_ROUTE
        if sitting_date_iso > _SPRS3_BOUNDARY_ISO
        else _SPRS2_REPORT_ROUTE
    )
    return (
        f"{_settings.upstream_public_base}{route}"
        f"?sittingdate={d.day}-{d.month:02d}-{d.year}"
    )


def from_result_html(
    result_html: dict[str, Any], *, source_url: str
) -> TopicPayload:
    """Build a TopicPayload from a raw ``resultHTML`` dict (either era)."""
    return TopicPayload(
        report_version=_opt_str(result_html.get(_REPORT_VERSION_FIELD)),
        html_content=_opt_str(result_html.get(_HTML_CONTENT_FIELD)),
        content=_opt_str(result_html.get(_CONTENT_FIELD)),
        report_id=_opt_str(result_html.get("reportId"))
        or _opt_str(result_html.get("htmlFileName"))
        or "",
        title=_opt_str(result_html.get("title")),
        sitting_date=_opt_str(result_html.get("sittingDate")),
        report_type=_opt_str(result_html.get("reportType")),
        volume_no=_opt_str(result_html.get("volumeNo")),
        parl_no=_opt_str(result_html.get("parlNo")),
        session_no=_opt_str(result_html.get("sessionNo")),
        sitting_no=_opt_str(result_html.get("sittingNo")),
        source_url=source_url,
    )


def parse_topic(
    payload: TopicPayload, *, report_id: str
) -> HansardReport:
    """Parse a TopicPayload via the branch matching its reportVersion.

    When ``reportVersion`` is absent (some sprs2 payloads), the presence of a
    full ``htmlContent`` document selects the sprs2 branch; otherwise sprs3.
    Raises ``ValueError`` when no branch can be determined (fail-closed —
    the app maps this to 502, never fabricates text).
    """
    version = payload.report_version
    if version is None:
        if payload.html_content:
            version = SPRS2
        elif payload.content:
            version = SPRS3
        else:
            raise ValueError("topic payload has neither htmlContent nor content")
    # Imported here to break the payload↔parser import cycle at module load.
    from hansard_gateway.sprs import current as _current
    from hansard_gateway.sprs import legacy as _legacy

    parsers: dict[str, Callable[..., HansardReport]] = {
        SPRS2: _legacy.parse,
        SPRS3: _current.parse,
    }
    parser = parsers.get(version)
    if parser is None:
        raise ValueError(f"unknown reportVersion: {version!r}")
    return parser(payload, report_id=report_id)
