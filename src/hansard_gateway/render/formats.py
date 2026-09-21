"""Format serializers (spec §12) — JSON / plain-text renderings, split from
:mod:`render.__init__` for the 300-LOC rule (Phase 27.1 wave 6).

Pure over the normalized models; every URL built by the abs_*_url helpers.
The search serializer carries the F-4 honest results header
(:func:`results_line`) instead of a bare upstream estimate.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any

from hansard_gateway.models import HansardReport, SearchHit, SearchPage
from hansard_gateway.render.urls import abs_date_url, abs_report_url

#: The results header for the search serializer (F-4 honest count). Imported
#: here (not in __init__) so the format path does not pull the HTML render.
from hansard_gateway.render import results_line


def _json_default(value: Any) -> Any:
    """JSON encoder hook for dataclass / date fields."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"not JSON-serializable: {type(value)!r}")


def _hit_json(hit: SearchHit, *, token: str) -> dict[str, Any]:
    """Serialize one SearchHit with an absolute token-bearing report url."""
    data = asdict(hit)
    data["url"] = abs_report_url(token=token, link_id=hit.link_id)
    return data


def render_report_format(*, report: HansardReport, fmt: str, token: str) -> str:
    """Serialize a report as JSON or plain text (spec §12)."""
    if fmt == "json":
        data = asdict(report)
        data["url"] = abs_report_url(token=token, link_id=report.report_id)
        data["links"] = {
            "self": abs_report_url(token=token, link_id=report.report_id),
            "sitting": abs_date_url(token=token, day_iso=report.date.isoformat()),
        }
        # v0.1.7 c9 (approved additive amendment): EVERY speech gains
        # ``speech_id`` + ``cite_url`` REGARDLESS of the HTML Cite cap (the
        # cap limits rendered PRESENTATION; JSON addressability is
        # uncapped). cite_url calls the SAME abs_report_url helper the HTML
        # Cite line uses (single source of truth — NOT the cap-aware
        # build_cite_context). The two keys are APPENDED after the existing
        # speech keys (no reordering); opening a cite_url focuses +
        # gold-highlights the WHOLE speech article (article.speech:target),
        # not a per-speech extraction.
        for speech in data["speeches"]:
            sequence = speech["sequence"]
            speech_id = f"speech-{sequence}"
            speech["speech_id"] = speech_id
            speech["cite_url"] = abs_report_url(
                token=token, link_id=report.report_id, fragment=speech_id,
            )
        return json.dumps(data, default=_json_default, indent=2)
    lines = [
        report.title,
        f"Date: {report.date.isoformat()}",
        f"Report ID: {report.report_id}",
        f"Source: Singapore Parliamentary Reports",
        f"Official SPRS record: {report.source_url}",
        f"Gateway URL: /a/{token}/report/{report.report_id}",
        "",
    ]
    for speech in report.speeches:
        speaker = speech.speaker_original or "[procedural]"
        lines.append(f"== {speaker} ==")
        lines.extend(speech.paragraphs)
        lines.append("")
    return "\n".join(lines)


def render_search_format(*, page: SearchPage, fmt: str, token: str = "") -> str:
    """Serialize a search page as JSON or plain text (spec §12).

    The plain-text header is the F-4 honest count (rendered vs estimate),
    not a bare ``total``."""
    if fmt == "json":
        data = asdict(page)
        data["hits"] = [_hit_json(h, token=token) for h in page.hits]
        return json.dumps(data, default=_json_default, indent=2)
    lines = [
        f"Query: {page.query}",
        results_line(page=page, hits=page.hits),
        f"Page: {page.page} (limit {page.limit})",
        "",
    ]
    for hit in page.hits:
        lines.append(f"- {hit.title}")
        if token:
            lines.append(f"  url: {abs_report_url(token=token, link_id=hit.link_id)}")
        else:
            lines.append(f"  link: /report/{hit.link_id}")
        if hit.date:
            lines.append(f"  date: {hit.date.isoformat()}")
        if hit.report_type:
            lines.append(f"  section: {hit.report_type}")
        if hit.speaker:
            lines.append(f"  speaker: {hit.speaker}")
        lines.append("")
    return "\n".join(lines)


def render_date_format(*, hits: list[SearchHit], date_iso: str, fmt: str, token: str = "") -> str:
    """Serialize a sitting TOC as JSON or plain text (spec §12)."""
    if fmt == "json":
        return json.dumps(
            {"date": date_iso, "hits": [_hit_json(h, token=token) for h in hits]},
            default=_json_default,
            indent=2,
        )
    lines = [f"Sitting: {date_iso}", f"Reports: {len(hits)}", ""]
    for hit in hits:
        lines.append(f"- {hit.title}")
        if token:
            lines.append(f"  url: {abs_report_url(token=token, link_id=hit.link_id)}")
        else:
            lines.append(f"  link: /report/{hit.link_id}")
    return "\n".join(lines)
