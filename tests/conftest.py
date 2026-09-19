"""Shared pytest fixtures for the sg-hansard-gateway test suite."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from hansard_gateway.auth import TEST_TOKEN, TEST_TOKEN_LABEL, TokenStore
from hansard_gateway.config import Settings
from hansard_gateway.index.build import build_index
from hansard_gateway.index.loader import IndexService
from hansard_gateway.rate_limit import RateGate

#: Root of the committed upstream-payload fixture directory (offline ground truth).
FIXTURES_DIR: Path = Path(__file__).parent / "fixtures"


class FakeClock:
    """Injectable monotonic clock for deterministic rate-limit tests."""

    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture()
def fixtures_dir() -> Path:
    """Return the path of the tests/fixtures/ directory."""
    return FIXTURES_DIR


@pytest.fixture()
def settings() -> Settings:
    """A default Settings instance (all limits at their locked values)."""
    return Settings()


@pytest.fixture()
def fake_clock() -> FakeClock:
    """An injectable monotonic clock starting at t=0."""
    return FakeClock()


@pytest.fixture()
def token_store(tmp_path: Path) -> TokenStore:
    """A TokenStore with one enabled token + one disabled (revoked) token."""
    path = tmp_path / "tokens.yaml"
    entries = [
        {
            "label": TEST_TOKEN_LABEL,
            "sha256": hashlib.sha256(TEST_TOKEN.encode("utf-8")).hexdigest(),
            "enabled": True,
            "last4": "cdef",
        },
        {
            "label": "hansard-team-revoked",
            "sha256": hashlib.sha256(b"hg_revokedtoken0123456789abcdef").hexdigest(),
            "enabled": False,
            "last4": "cdef",
        },
    ]
    path.write_text("tokens:\n" + _yaml_entries(entries), encoding="utf-8")
    return TokenStore(path=path)


def _yaml_entries(entries: list[dict[str, object]]) -> str:
    """Render token entries as YAML without importing a yaml dumper."""
    lines: list[str] = []
    for e in entries:
        lines.append(f"- label: {e['label']}")
        lines.append(f"  sha256: {e['sha256']}")
        lines.append(f"  enabled: {str(e['enabled']).lower()}")
        lines.append(f"  last4: {e['last4']}")
    return "\n".join(lines) + "\n"


@pytest.fixture()
def rate_gate(settings: Settings, fake_clock: FakeClock) -> RateGate:
    """A RateGate wired to the fake clock for deterministic limits."""
    return RateGate(settings=settings, clock=fake_clock)


def _fixture_report_rows() -> list[dict]:
    """~20 hand-made report rows spanning several letters + a bill term.

    Includes 'Health Information Bill' (kind=bill) and topics/speakers across
    letters so the ladder/facet queries have something to find.
    """
    rows = [
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
    return rows


@pytest.fixture()
def index(tmp_path: Path) -> IndexService:
    """A tiny tmp SQLite index + an IndexService pointed at it (wave 2/3 data)."""
    target = tmp_path / "index.db"
    build_index(_fixture_report_rows(), target)
    return IndexService(path=target)


@pytest.fixture()
def app_with_index(token_store, index: IndexService):
    """The full app with the fixture index injected (no real ~/.hansard)."""
    import hansard_gateway.auth as auth_mod

    auth_mod._store = token_store
    from hansard_gateway.main import create_app

    return create_app(index_override=index)


@pytest.fixture()
def client_with_index(app_with_index):
    """A TestClient over the app carrying the fixture index."""
    from fastapi.testclient import TestClient

    return TestClient(app_with_index)


# --- rich index (Phase 27.1 wave 5, spec 8.1 acceptance corpus) -----------------


#: Spec 8.1 target 5: the >100-hit search's row count (3 pages at limit 50).
RICH_HIT_COUNT = 120

#: Spec 8.1 target 5: the query the row builder keys its corpus on.
RICH_HIT_QUERY = "Budget"

#: Spec 8.1 target 3: the 1988 sitting whose TOC must be reachable.
RICH_SITTING_1988 = "1988-03-02"

#: Spec 8.1 target 4: the named backbencher who speaks in the 1988 sitting.
RICH_BACKBENCHER = "Lim Hwee Hua"


def _rich_hit_rows() -> list[dict]:
    """The RICH_HIT_COUNT synthetic rows for the >100-hit search (target 5).

    Each row is a distinct topic+speaker pair so the index sees 240 terms
    (spec 8.4 samples 500 of the index's ~270 terms). Row rh000 sits in the
    1988 sitting and carries the named backbencher (target 4). The rh001+
    rows sit TODAY (the sweep's date_to defaults to date.today() — a row
    dated in the future would be excluded, and the index's recent_sittings
    would push the 1988 sitting off the launcher's 30-date list).
    """
    rows: list[dict] = []
    today = date.today().isoformat()
    for i in range(RICH_HIT_COUNT):
        rows.append(
            {
                "report_id": f"rh{i:03d}",
                "link_id": f"rh{i:03d}",
                "sitting_date": (
                    RICH_SITTING_1988
                    if i == 0
                    else "1988-03-09" if i == 1 else today
                ),
                "title": f"Budget Review Number {i}",
                "report_type": "oral-answer",
                "speaker": RICH_BACKBENCHER if i == 0 else None,
            }
        )
    return rows


def _rich_fixture_report_rows() -> list[dict]:
    """The spec 8.1 acceptance corpus (one hand-made block + the 120 hit rows).

    Contains every entity the six reachability targets need: the HIB bill term,
    the second-word-distinctive topic, the 1988 sitting, the named backbencher,
    and the >100-hit search corpus. The wave-1 ``index`` fixture is untouched
    (other waves' tests depend on its shape) — this is a separate corpus.
    """
    rows = [
        # HIB bill term (target 1: kind=bill, several reports). NOTE: the
        # extract rule reads `reportType` from the row — report_type="bill"
        # rows get NO bill term (the bill term comes from the title regex on
        # non-bill rows, per the real SPRS row shape), so these use
        # oral-answer/written-answer like the real HIB reports.
        {"report_id": "hib1", "link_id": "hib1", "sitting_date": "2026-01-12",
         "title": "Health Information Bill", "report_type": "oral-answer",
         "speaker": None},
        {"report_id": "hib2", "link_id": "hib2", "sitting_date": "2026-01-13",
         "title": "Health Information Bill (Amendment No. 1)",
         "report_type": "oral-answer", "speaker": None},
        {"report_id": "hib3", "link_id": "hib3", "sitting_date": "2026-01-14",
         "title": "Health Information Bill (Second Reading)",
         "report_type": "written-answer", "speaker": None},
        # Second-word-distinctive topic (target 2): 'Review' is the salient
        # word; 'Budget' is the generic one.
        {"report_id": "rd1", "link_id": "rd1", "sitting_date": "2024-11-25",
         "title": "Budget Review", "report_type": "oral-answer",
         "speaker": None},
        # Bare 'Budget' topic (target 5: the >100-hit search's ladder link).
        {"report_id": "bud0", "link_id": "bud0", "sitting_date": "2024-11-25",
         "title": "Budget", "report_type": "oral-answer",
         "speaker": None},
        # 1988 sittings with reports (target 3 TOC; target 4's backbencher row
        # rh000 also sits on RICH_SITTING_1988).
        {"report_id": "f1988", "link_id": "f1988", "sitting_date": RICH_SITTING_1988,
         "title": "Fisheries and Marine Policy", "report_type": "oral-answer",
         "speaker": None},
        {"report_id": "f1988b", "link_id": "f1988b", "sitting_date": "1988-03-09",
         "title": "Harbour Front Redevelopment", "report_type": "written-answer",
         "speaker": None},
        # Extra sittings (prev/next-sitting links on the 1988 TOC).
        {"report_id": "x1987", "link_id": "x1987", "sitting_date": "1987-06-10",
         "title": "Postal Services Modernisation", "report_type": "oral-answer",
         "speaker": None},
        {"report_id": "x1989", "link_id": "x1989", "sitting_date": "1989-01-04",
         "title": "Air Traffic Control Upgrades", "report_type": "written-answer",
         "speaker": None},
    ]
    rows.extend(_rich_hit_rows())
    return rows


@pytest.fixture()
def rich_index(tmp_path: Path) -> IndexService:
    """The spec 8.1 acceptance corpus built via wave-1 index.build."""
    target = tmp_path / "rich_index.db"
    build_index(_rich_fixture_report_rows(), target)
    return IndexService(path=target)
