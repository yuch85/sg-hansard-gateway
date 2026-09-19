"""Unit tests for the SPRS provenance URL (topic_source_url + _sitting_iso).

SPRS is an Angular SPA with no public per-report page (``/hansard/<id>``
404s); the provenance URL must point at the sitting route the report
belongs to — pre-2013 sits at ``#/report``, post-2012 sits at
``#/fullreport`` (both verified live 2026-09-19, see
``missions/006-.../logs/sprs-url-bug-diagnosis.md``).
"""

from __future__ import annotations

import pytest

from hansard_gateway.report_handler import _sitting_iso
from hansard_gateway.sprs.payload import topic_source_url


class TestTopicSourceUrl:
    """The URL builder picks the silo route from the sitting date."""

    def test_sprs2_sit_uses_report_route(self) -> None:
        url = topic_source_url(
            report_id="037_20041019_S0004_T0023", sitting_date_iso="2004-10-19"
        )
        assert url == (
            "https://sprs.parl.gov.sg/search/#/report?sittingdate=19-10-2004"
        )

    def test_sprs3_sit_uses_fullreport_route(self) -> None:
        url = topic_source_url(report_id="bill-742", sitting_date_iso="2025-01-08")
        assert url == (
            "https://sprs.parl.gov.sg/search/#/fullreport?sittingdate=8-01-2025"
        )

    def test_boundary_date_stays_on_sprs2_route(self) -> None:
        # 2012-09-10 is the SPA's silo boundary day itself: the app treats
        # dates BEFORE it as sprs2 (selectedDate >= sprs2Date -> sprs3).
        url = topic_source_url(report_id="x", sitting_date_iso="2012-09-10")
        assert "/#/report?sittingdate=" in url
        assert "/#/fullreport" not in url

    def test_day_after_boundary_moves_to_fullreport(self) -> None:
        url = topic_source_url(report_id="x", sitting_date_iso="2012-09-11")
        assert url.endswith("/#/fullreport?sittingdate=11-09-2012")

    def test_no_dead_hansard_scheme(self) -> None:
        # The old /hansard/<id> URL 404s — it must never be emitted.
        url = topic_source_url(report_id="017_19931012_S0003_T0003",
                               sitting_date_iso="1993-10-12")
        assert "/hansard/" not in url
        assert "017_19931012_S0003_T0003" not in url

    def test_no_capability_token_in_url(self) -> None:
        url = topic_source_url(report_id="bill-742", sitting_date_iso="2025-01-08")
        assert "hg_" not in url


class TestSittingIso:
    """Sitting-date extraction from raw resultHTML payloads."""

    def test_sprs3_top_level_date(self) -> None:
        assert _sitting_iso(result_html={"sittingDate": "8-1-2025"}) == "2025-01-08"

    def test_sprs2_meta_iso_date(self) -> None:
        html = (
            "<html><head>"
            '<meta name="Sit_Date" content="2004-10-19">'
            "</head><body>x</body></html>"
        )
        assert _sitting_iso(result_html={"htmlContent": html}) == "2004-10-19"

    def test_sprs2_meta_date_format_variant(self) -> None:
        html = '<meta name="sit_date" content="12-10-1993">'
        assert _sitting_iso(result_html={"htmlContent": html}) == "1993-10-12"

    def test_missing_date_degrades_to_min(self) -> None:
        assert _sitting_iso(result_html={}) == "0001-01-01"

    def test_unparseable_date_degrades_to_min(self) -> None:
        assert _sitting_iso(result_html={"sittingDate": "nonsense"}) == "0001-01-01"

    def test_top_level_wins_over_meta(self) -> None:
        html = '<meta name="Sit_Date" content="2004-10-19">'
        assert _sitting_iso(
            result_html={"sittingDate": "8-1-2025", "htmlContent": html}
        ) == "2025-01-08"
