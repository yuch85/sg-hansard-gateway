"""SPRS request-body builders (spec §3.1) — split from :mod:`client` for the
300-LOC rule (Phase 27.1 wave 6, F-5b).

Single source of truth for the upstream contract: the offline crawl
(``crawl/crawl_client.py``) and the live client both build the 21-field
searchResult body here, so a body change lands in one place. The constants
describe the verified-working browser-like request (RESEARCH Finding 2).
"""

from __future__ import annotations

from typing import Any

#: Constant strings for the verified-working searchResult body (Finding 2).
_REPORT_CONTENT_MODE = "with all the words"
_SELECTED_SORT = "date_dt desc"
_DAY_START_SUFFIX = "T00:00:00Z"
_DAY_END_SUFFIX = "T23:59:59Z"
_RANGE_SEP = " TO "


def build_search_body(
    *,
    keyword: str,
    date_from: str,
    date_to: str,
    mp_name: str,
    start_index: int,
    end_index: int,
) -> dict[str, Any]:
    """Build the 21-field searchResult body (Finding 2) — public re-export.

    Single source of truth for the upstream contract: the offline crawl
    imports this instead of copying the dict, so a body change lands in one
    place (Phase 27.1, spec §3.1).
    """
    return {
        "keyword": keyword,
        "reportContent": _REPORT_CONTENT_MODE,
        "parliamentNo": "",
        "selectedSort": _SELECTED_SORT,
        "portfolio": [],
        "mpName": mp_name,
        "rsSelected": "",
        "lang": "",
        "startIndex": str(start_index),
        "endIndex": str(end_index),
        "titleChecked": "false",
        "footNoteChecked": "false",
        "ministrySelected": [],
        "fromday": "",
        "frommonth": "",
        "fromyear": "",
        "today": "",
        "tomonth": "",
        "toyear": "",
        "dateRange": format_day_range(date_from=date_from, date_to=date_to),
    }


def format_day_range(*, date_from: str, date_to: str) -> str:
    """Format the Solr day-range string (``{from}T00:00:00Z TO {to}T23:59:59Z``)."""
    return (
        f"{date_from}{_DAY_START_SUFFIX}"
        f"{_RANGE_SEP}"
        f"{date_to}{_DAY_END_SUFFIX}"
    )
