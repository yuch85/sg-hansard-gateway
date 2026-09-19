"""Scaffold smoke tests: package import + fixture ground-truth integrity."""

from __future__ import annotations

import json
from pathlib import Path

import hansard_gateway

import pytest


@pytest.fixture()
def fixtures_dir() -> Path:
    """Return the path of the tests/fixtures/ directory."""
    return Path(__file__).parent / "fixtures"


def test_package_version() -> None:
    """The package imports and reports its version."""
    assert hansard_gateway.__version__ == "0.1.0"


def test_topic_fixture_spr2_flat_htmlcontent(fixtures_dir: Path) -> None:
    """sprs2 topic payload is a FLAT object: htmlContent at the root."""
    payload = json.loads(
        (fixtures_dir / "topic_20041019_saf.json").read_text(encoding="utf-8")
    )
    html_content = payload["htmlContent"]
    assert len(html_content) >= 10_000
    assert "MP_NAME:" in html_content
    assert "SINGAPORE ARMED FORCES" in html_content


def test_topic_fixture_spr3_resulthtml_content(fixtures_dir: Path) -> None:
    """sprs3 topic payload nests the transcript under resultHTML.content."""
    payload = json.loads(
        (fixtures_dir / "topic_20250108_bill742.json").read_text(encoding="utf-8")
    )
    content = payload["resultHTML"]["content"]
    assert "<strong>" in content


def test_searchresult_fixture_shapes(fixtures_dir: Path) -> None:
    """Both searchResult fixtures are arrays of 46-field row objects."""
    rows_2004 = json.loads(
        (fixtures_dir / "searchresult_20041019_p1.json").read_text(encoding="utf-8")
    )
    rows_2025 = json.loads(
        (fixtures_dir / "searchresult_20250108_p1.json").read_text(encoding="utf-8")
    )
    assert len(rows_2004) == 20
    assert len(rows_2025) == 20
    for rows in (rows_2004, rows_2025):
        for row in rows:
            assert isinstance(row, dict) and len(row) == 46
