"""Unit tests for report-ID validation, date derivation, normalization."""

from __future__ import annotations

from datetime import date

import pytest

from hansard_gateway.report_id import (
    ResolutionPlan,
    ResolutionStrategy,
    derive_date,
    normalize_for_topic,
    resolution_plan,
    validate_report_id,
)

SPEC_ID = "037_20041019_S0004_T0023"
LIVE_ID_1999_EMBEDDED = "00000344-XA.00066597-ZZ_1#id029_19990120_S0003_T00031-bill#"
LIVE_ID_NO_DATE = "bill-742"


class TestValidateReportId:
    """The charset/length guard accepts both ID systems and rejects traversal."""

    @pytest.mark.parametrize(
        "report_id",
        [SPEC_ID, LIVE_ID_NO_DATE, "a", "_"],
    )
    def test_accepts_valid_grammars(self, report_id: str) -> None:
        assert validate_report_id(report_id=report_id) is True

    @pytest.mark.parametrize(
        "report_id",
        [
            "../etc/passwd",
            "a/b",
            "foo#bar",
            "bill-742#",
            "00075526-WA.00070465-ZZ_1",
            "x" * 101,
            "",
            "id with space",
            "id.dot",
        ],
    )
    def test_rejects_traversal_and_out_of_charset(self, report_id: str) -> None:
        assert validate_report_id(report_id=report_id) is False


class TestDeriveDate:
    """Date comes only from an 8-digit YYYYMMDD segment; None otherwise."""

    def test_spec_id_derives_sitting_date(self) -> None:
        assert derive_date(report_id=SPEC_ID) == date(2004, 10, 19)

    def test_embedded_1999_id_derives_its_date(self) -> None:
        assert derive_date(report_id=LIVE_ID_1999_EMBEDDED) == date(1999, 1, 20)

    def test_live_id_without_date_returns_none(self) -> None:
        assert derive_date(report_id=LIVE_ID_NO_DATE) is None

    def test_eight_digits_but_invalid_calendar_date_returns_none(self) -> None:
        assert derive_date(report_id="x20041399y") is None


class TestNormalizeForTopic:
    """Trailing # stripping so getHansardTopic accepts live row IDs."""

    def test_strips_single_trailing_hash(self) -> None:
        assert normalize_for_topic(report_id="bill-742#") == "bill-742"

    def test_strips_double_trailing_hash(self) -> None:
        assert (
            normalize_for_topic(report_id="00075526-WA.00070465-ZZ_1##")
            == "00075526-WA.00070465-ZZ_1"
        )

    def test_no_hash_is_identity(self) -> None:
        assert normalize_for_topic(report_id=LIVE_ID_NO_DATE) == LIVE_ID_NO_DATE


class TestResolutionPlan:
    """The plan names the strategy, candidates, and browse date."""

    def test_spec_id_plans_direct_topic_with_date(self) -> None:
        plan: ResolutionPlan = resolution_plan(report_id=SPEC_ID)
        assert plan.strategy is ResolutionStrategy.DIRECT_TOPIC
        assert plan.candidate_ids == [SPEC_ID]
        assert plan.browse_date == date(2004, 10, 19)

    def test_embedded_1999_id_normalizes_and_derives_date(self) -> None:
        # The raw live-row form carries an interior # (a searchResult data ID,
        # not a user-supplied URL path param — the locked guard regex rejects
        # #). Normalization strips the trailing # and date-derivation still
        # recovers the embedded 19990120 sitting date.
        assert normalize_for_topic(report_id=LIVE_ID_1999_EMBEDDED) == (
            "00000344-XA.00066597-ZZ_1#id029_19990120_S0003_T00031-bill"
        )
        assert derive_date(report_id=LIVE_ID_1999_EMBEDDED) == date(1999, 1, 20)
        # The raw form is not a valid /report/{id} path param (# rejected).
        assert validate_report_id(report_id=LIVE_ID_1999_EMBEDDED) is False

    def test_live_id_without_hash_plans_browse_fallback(self) -> None:
        plan = resolution_plan(report_id=LIVE_ID_NO_DATE)
        assert plan.strategy is ResolutionStrategy.DATE_BROWSE
        assert plan.candidate_ids == [LIVE_ID_NO_DATE]
        assert plan.browse_date is None

    def test_invalid_id_plans_not_found(self) -> None:
        plan = resolution_plan(report_id="../etc/passwd")
        assert plan.strategy is ResolutionStrategy.NOT_FOUND
        assert plan.candidate_ids == []
