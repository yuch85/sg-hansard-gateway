"""Unit tests for the SPRS provenance URL (topic_source_url + _sitting_iso).

SPRS is an Angular SPA (mission 008, verified in a real browser 2026-09-20):
the provenance URL points at the SECTION route for the report — legacy ids
(``026_19950301_S0002_T0009``) at ``#/topic?reportid=``, live ids
(``bill-773``) at ``#/sprs3topic?reportid=`` — not the full-sitting routes
(``#/report`` / ``#/fullreport``), which remain the fallback only.
``/hansard/<id>`` 404s and must never be emitted.
"""

from __future__ import annotations

import pytest

from hansard_gateway.report_handler import _sitting_iso
from hansard_gateway.sprs.payload import topic_source_url

BASE = "https://sprs.parl.gov.sg/search/"


class TestTopicSourceUrl:
    """The URL builder picks the SECTION route from the id shape / era."""

    def test_spec_style_id_uses_sprs2_topic_route(self) -> None:
        url = topic_source_url(
            report_id="037_20041019_S0004_T0023", sitting_date_iso="2004-10-19"
        )
        assert url == f"{BASE}#/topic?reportid=037_20041019_S0004_T0023"

    def test_live_id_uses_sprs3_topic_route(self) -> None:
        url = topic_source_url(report_id="bill-742", sitting_date_iso="2025-01-08")
        assert url == f"{BASE}#/sprs3topic?reportid=bill-742"

    def test_pension_fund_bill_example(self) -> None:
        # Mission 008's example (YC's 1995 Pension Fund Bill section).
        url = topic_source_url(
            report_id="026_19950301_S0002_T0009", sitting_date_iso="1995-03-01"
        )
        assert url == f"{BASE}#/topic?reportid=026_19950301_S0002_T0009"

    def test_boundary_date_stays_on_sprs2_topic_route(self) -> None:
        # 2012-09-10 is the SPA's silo boundary day itself: the app treats
        # dates BEFORE it as sprs2 (selectedDate >= sprs2Date -> sprs3).
        url = topic_source_url(report_id="x", sitting_date_iso="2012-09-10")
        assert url == f"{BASE}#/topic?reportid=x"

    def test_day_after_boundary_moves_to_sprs3_topic_route(self) -> None:
        url = topic_source_url(report_id="x", sitting_date_iso="2012-09-11")
        assert url == f"{BASE}#/sprs3topic?reportid=x"

    def test_no_dead_hansard_scheme(self) -> None:
        # The old /hansard/<id> URL 404s — it must never be emitted.
        url = topic_source_url(report_id="017_19931012_S0003_T0003",
                               sitting_date_iso="1993-10-12")
        assert "/hansard/" not in url

    def test_unknown_shape_unparsable_date_falls_back_to_full_report(self) -> None:
        # Neither era's topic route can be trusted (no id shape, no date) —
        # degrade to the full-sitting route (pre-mission-008 behaviour).
        url = topic_source_url(report_id="weird-id", sitting_date_iso="nonsense")
        assert url == f"{BASE}#/report?sittingdate=1-01-0001"

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
