"""The frozen page corpus for the machine-surface baselines (Phase 27.3).

Holds the 8-entry corpus definition, the per-entry route-path helpers, and the
offline test-index rows. Kept separate from ``machine_baseline.py`` (the
capture/verify driver) so each file stays under the 300-LOC STYLE.md cap.

The corpus is the a1 corpus (confirmed live in 27.3-01 Task 1): launcher,
search HIB p1, report bill-774, nav/h, years, members, bills, date 2026-01-12.

The :data:`CORPUS` entries declare their capture ``source``:

* ``"offline"`` — the 6 index-driven entries, rendered in-process against the
  committed test index (deterministic, reproducible — the real regression
  target the offline suite diffs against).
* ``"live"`` — the 2 upstream-stubbed entries (report, search), fetched from
  the healthy production deployment (the offline index cannot reproduce their
  content; the offline test asserts structural invariants, full equality is
  asserted live by ``machine_baseline.run_verify``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True)
class CorpusEntry:
    """One page in the frozen corpus.

    ``name`` is the fixture file stem (e.g. ``report_bill-774``). ``path`` is
    the route path *after* ``/a/{token}`` (e.g. ``/report/bill-774``).
    ``source`` is ``"offline"`` or ``"live"``. ``query`` is the search query
    for the search entry (empty otherwise). ``upstream_stub`` marks an
    offline entry whose route still hits the SPRS upstream (the date TOC does a
    sitting sweep) — the offline capture respx-stubs it with the committed
    searchResult fixture so it is deterministic.
    """

    name: str
    path: str
    source: str  # "offline" | "live"
    query: str = ""
    upstream_stub: bool = False


#: The 8-entry corpus (the a1 corpus, confirmed live in Task 1).
CORPUS: tuple[CorpusEntry, ...] = (
    CorpusEntry(name="launcher", path="/", source="offline"),
    CorpusEntry(name="search_hib_p1", path="/search", source="live",
                query="Health Information Bill"),
    CorpusEntry(name="report_bill-774", path="/report/bill-774", source="live"),
    CorpusEntry(name="nav_h", path="/nav/h", source="offline"),
    CorpusEntry(name="years", path="/years", source="offline"),
    CorpusEntry(name="members", path="/members", source="offline"),
    CorpusEntry(name="bills", path="/bills", source="offline"),
    # The date TOC hits the upstream (sweep_sitting -> searchResult) even
    # though it is index-adjacent; respx-stub it offline for determinism.
    CorpusEntry(name="date_2026-01-12", path="/date/2026-01-12",
                source="offline", upstream_stub=True),
)


def entry_path(entry: CorpusEntry) -> str:
    """The full route path for an entry (search appends the encoded query)."""
    if entry.query:
        return f"{entry.path}?q={quote(entry.query, safe='')}"
    return entry.path


def text_path(entry: CorpusEntry) -> str:
    """The route path with ``?format=text`` (or ``&format=text``) appended."""
    sep = "&" if "?" in entry_path(entry) else "?"
    return f"{entry_path(entry)}{sep}format=text"


def offline_index_rows() -> list[dict[str, Any]]:
    """The committed test-index rows (mirrors conftest._fixture_report_rows).

    Kept local (not imported from conftest) so the capture script runs
    standalone without pytest. Must stay in sync with the conftest fixture the
    offline regression test renders against.
    """
    return [
        {"report_id": "b1", "link_id": "b1", "sitting_date": "2023-05-10",
         "title": "Health Information Bill", "report_type": "bill",
         "speaker": None},
        {"report_id": "b2", "link_id": "b2", "sitting_date": "2024-02-20",
         "title": "Health Information Bill (Amendment No. 2)",
         "report_type": "bill", "speaker": None},
        {"report_id": "t1", "link_id": "t1", "sitting_date": "2025-01-08",
         "title": "Use of Bus Boarding Ramps", "report_type": "written-answer",
         "speaker": "Tan Chun Seng"},
        {"report_id": "t2", "link_id": "t2", "sitting_date": "2025-01-08",
         "title": "Data Protection and Cybersecurity", "report_type": "oral-answer",
         "speaker": "Tan Chun Seng"},
        {"report_id": "t3", "link_id": "t3", "sitting_date": "2020-03-01",
         "title": "Economic Recovery and Jobs", "report_type": "oral-answer",
         "speaker": "Alice Tan"},
        {"report_id": "t4", "link_id": "t4", "sitting_date": "2020-03-01",
         "title": "Economic Recovery and Jobs", "report_type": "written-answer",
         "speaker": None},
        {"report_id": "t5", "link_id": "t5", "sitting_date": "1988-11-15",
         "title": "Fisheries and Marine Policy", "report_type": "oral-answer",
         "speaker": "Foo Ah Kow"},
        {"report_id": "t6", "link_id": "t6", "sitting_date": "1988-11-15",
         "title": "Fisheries and Marine Policy", "report_type": "bill",
         "speaker": None},
        {"report_id": "t7", "link_id": "t7", "sitting_date": "2001-06-20",
         "title": "Government Accounting and Audit", "report_type": "written-answer",
         "speaker": None},
        {"report_id": "t8", "link_id": "t8", "sitting_date": "2001-06-20",
         "title": "Government Accounting and Audit", "report_type": "oral-answer",
         "speaker": "Heng Chee How"},
    ]
