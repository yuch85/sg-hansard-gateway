"""Tests for the offline term index: schema, extraction, prefix build."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hansard_gateway.config import Settings
from hansard_gateway.index import build, extract, schema


def _row(
    *,
    report_id: str,
    title: str,
    report_type: str | None = None,
    speaker: str | None = None,
    sitting_date: str = "2025-01-08",
) -> dict:
    """A report-shaped row dict for the builder (metadata-only)."""
    return {
        "report_id": report_id,
        "link_id": report_id.rstrip("#"),
        "sitting_date": sitting_date,
        "title": title,
        "report_type": report_type,
        "speaker": speaker,
    }


def _make_rows() -> list[dict]:
    """A small corpus covering bills, topics, speakers, and a fat branch.

    'Budget Review' exercises the per-word child fan-out: 'review' is the
    term's SECOND word, so 're' must appear as a child of 'r' (spec §3.4/§8.4
    — a term is reachable from EVERY word it contains)."""
    rows = [
        _row(report_id="r1", title="Health Information Bill",
             report_type="bill", sitting_date="2023-05-10"),
        _row(report_id="r2", title="Health Information Bill (Amendment No. 2)",
             report_type="bill", sitting_date="2024-02-20"),
        _row(report_id="r3", title="Written Answer: Use of Bus Boarding Ramps",
             report_type="written-answer", speaker="Tan Chun Seng"),
        _row(report_id="r4", title="Head B - Question No. 12: Data Protection",
             report_type="oral-answer", speaker="Tan Chun Seng"),
        _row(report_id="r5", title="Budget Review",
             report_type="oral-answer", sitting_date="2024-11-25"),
        # A fat branch: 2500 terms sharing the prefix "zeta".
    ]
    for i in range(2500):
        rows.append(
            _row(
                report_id=f"z{i}",
                title=f"Zeta Topic {i}",
                report_type="oral-answer",
                sitting_date="2020-01-01",
            )
        )
    return rows


@pytest.fixture()
def built_index(tmp_path: Path) -> Path:
    """Build a tiny index over _make_rows into a tmp file."""
    cfg = Settings(fat_branch_threshold=2000)
    target = tmp_path / "index.db"
    build.build_index(_make_rows(), target, settings=cfg)
    return target


def test_norm_strips_and_collapses() -> None:
    assert extract.norm("  Written--Answer: Health (Bill)!! ") == (
        "written answer health bill"
    )
    # NFKC: fullwidth chars fold to ASCII.
    assert extract.norm("Ｈｅａｌｔｈ　Ｂｉｌｌ") == "health bill"


def test_bill_regex_extracts_name() -> None:
    assert extract.bill_name("Health Information Bill") == "Health Information"
    assert extract.bill_name("Health Information Bill (Amendment No. 2)") == (
        "Health Information"
    )
    assert extract.bill_name("Income Tax (Amendment) Bill") == "Income Tax"
    assert extract.bill_name("Written Answer: Health (Bill)") is None


def test_bill_kind_from_extraction() -> None:
    terms = extract.extract_terms(
        [_row(report_id="x", title="Health Information Bill", report_type="bill")]
    )
    by_norm = {t.norm: t for t in terms}
    assert by_norm["health information"].kind == "bill"
    assert by_norm["health information"].surface == "Health Information"


def test_topic_noise_stripped() -> None:
    terms = extract.extract_terms(
        [
            _row(
                report_id="y",
                title="Head B - Question No. 12: Data Protection",
                report_type="oral-answer",
            )
        ]
    )
    by_norm = {t.norm: t for t in terms}
    assert "data protection" in by_norm
    assert by_norm["data protection"].kind == "topic"
    # The noise prefix must not survive.
    assert "head b question no 12 data protection" not in by_norm


def test_none_speaker_is_legitimate() -> None:
    # Recent rows have mpNames null — no speaker term, no error.
    terms = extract.extract_terms(
        [_row(report_id="n", title="Some Topic", report_type="written-answer",
              speaker=None)]
    )
    assert all(t.kind != "speaker" for t in terms)


def test_short_terms_dropped() -> None:
    terms = extract.extract_terms(
        [_row(report_id="s", title="AI Bill", report_type="bill")]
    )
    by_norm = {t.norm: t for t in terms}
    assert "ai" not in by_norm  # 2 chars < MIN_TERM_LEN
    assert "ai" not in [t.surface for t in terms]


def test_stopword_gets_no_prefix_rows(built_index: Path) -> None:
    conn = sqlite3.connect(built_index)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM term_prefix WHERE prefix = 'the'"
        ).fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_cross_word_prefix_reachability(built_index: Path) -> None:
    """'Health Information Bill' reachable from heal/info/bill prefixes."""
    conn = sqlite3.connect(built_index)
    try:
        def surfaces(prefix: str) -> list[str]:
            return [
                s
                for (s,) in conn.execute(
                    "SELECT t.surface FROM term_prefix tp "
                    "JOIN term t ON t.term_id = tp.term_id "
                    "WHERE tp.prefix = ?",
                    (prefix,),
                )
            ]

        assert "Health Information Bill" in surfaces("heal")
        assert "Health Information Bill" in surfaces("info")
        assert "Health Information Bill" in surfaces("bill")
    finally:
        conn.close()


def _children_of(conn: sqlite3.Connection, prefix: str) -> dict[str, int]:
    """{child: n_terms} for one prefix (per-word fan-out, spec §3.4/§4.2)."""
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT child, n_terms FROM prefix_children WHERE prefix = ?",
            (prefix,),
        )
    }


def test_prefix_children_per_word_fanout(built_index: Path) -> None:
    """Children are per-word, not whole-norm (spec §3.4/§8.4).

    'Budget Review' (norm 'budget review') contributes 'bu' under 'b' AND
    're' under 'r' — the old substr(norm, 1, 2) fan-out only produced
    'bu' (the term's SECOND whole-norm character), so 're' was missing and a
    walk descending on the second word could not reach the term."""
    conn = sqlite3.connect(built_index)
    try:
        b_children = _children_of(conn, "b")
        assert b_children.get("bu", 0) >= 1, "expected child 'bu' under 'b'"
        r_children = _children_of(conn, "r")
        assert r_children.get("re", 0) >= 1, (
            "expected child 're' under 'r' (the second word of 'budget review')"
        )
        # Depth > 1 keeps working: 're' fans out to 'rev'.
        re_children = _children_of(conn, "re")
        assert re_children.get("rev", 0) >= 1, "expected child 'rev' under 're'"
    finally:
        conn.close()


def test_prefix_children_fat_branch(built_index: Path) -> None:
    """A fat child stores its grandchild; an ordinary child does not.

    Under the per-word semantics the fat branch is 'ze' (2500+ terms under
    the 'zeta...' titles), not the old whole-norm 'z'; 'he' stays ordinary."""
    conn = sqlite3.connect(built_index)
    try:
        # 'ze' is the fat child (2500+ terms) under 'z'.
        fat = conn.execute(
            "SELECT child, n_terms, grandchild, gc_n_terms "
            "FROM prefix_children WHERE prefix = 'z' AND child = 'ze'"
        ).fetchall()
        assert fat, "expected the fat child 'ze' under 'z'"
        # 'he' is an ordinary child.
        ordinary = conn.execute(
            "SELECT child, n_terms, grandchild, gc_n_terms "
            "FROM prefix_children WHERE prefix = 'h' AND child = 'he'"
        ).fetchall()
        assert ordinary, "expected an ordinary 'he' child under 'h'"
        assert ordinary[0][2] is None and ordinary[0][3] is None
        # Fat branch: the top grandchild must be populated.
        z_row = fat[0]
        assert z_row[2] is not None, "fat child must carry a grandchild"
        assert z_row[3] is not None and z_row[3] > 0
    finally:
        conn.close()


def test_report_table_has_no_body_column(built_index: Path) -> None:
    """Metadata-only invariant: no body/transcript column anywhere."""
    conn = sqlite3.connect(built_index)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(report)")]
        assert "body" not in cols
        assert "transcript" not in cols
    finally:
        conn.close()
    # Source-level assertion on the DDL.
    ddl = " ".join(schema.SCHEMA_STATEMENTS).lower()
    report_ddl = ddl[ddl.index("create table if not exists report"):]
    report_ddl = report_ddl[: report_ddl.index("create table", 10)]
    assert "body" not in report_ddl
    assert "transcript" not in report_ddl


def test_build_id_stamped(built_index: Path) -> None:
    conn = sqlite3.connect(built_index)
    try:
        bid = conn.execute(
            "SELECT value FROM meta WHERE key = 'build_id'"
        ).fetchone()
        assert bid is not None and bid[0]
    finally:
        conn.close()


def test_prefix_depth_capped(built_index: Path) -> None:
    """Prefixes longer than ladder_depth are never stored."""
    conn = sqlite3.connect(built_index)
    try:
        longest = conn.execute(
            "SELECT MAX(length(prefix)) FROM term_prefix"
        ).fetchone()[0]
        assert longest == Settings().ladder_depth
    finally:
        conn.close()
