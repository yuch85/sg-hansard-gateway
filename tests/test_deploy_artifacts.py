"""Deploy artifact checks (Phase 27.1 wave 5, Task 3; Phase 27.2 Plan 2).

Offline: assert the systemd --user crawl timer/service files, the RUNBOOK
cold-crawl section, the .gitignore index.db entries, and the config
index_db_path all exist with the required content. No live deploy in the
test (the actual enable/cold-crawl/restart is the YC human checkpoint).

The deploy artifacts are PLACEHOLDER TEMPLATES (Phase 27.2, 27.2-REQ-01):
machine-specific strings were generalized to the canonical vocabulary
below, so the pins assert the placeholder forms — and, belt-and-braces,
that no forbidden machine string survives in any deploy file.

Follows the test_scaffold.py convention for file-content assertions.
"""

from __future__ import annotations

from pathlib import Path

from hansard_gateway.config import Settings

#: Repo root (test files live in tests/).
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The deploy artifact directory (repo-relative).
_DEPLOY_DIR = Path(__file__).parent.parent / "deploy"
_SYSTEMD_DIR = _DEPLOY_DIR / "systemd"
_RUNBOOK = _DEPLOY_DIR / "RUNBOOK.md"
_GITIGNORE = Path(__file__).parent.parent / ".gitignore"

#: The index DB file name (gitignored + 0600 under ~/.hansard/).
_INDEX_DB_NAME = "index.db"

#: Canonical placeholder vocabulary for the deploy templates (27.2-REQ-01,
#: round-8 C2). Plan 4's live-cutover runbook must stay within this set;
#: <CONTAINER_HOST_PORT> is included now so the set is complete before
#: Plan 4. The two <HOME_HANSARD_*> entries are the recorded pattern
#: extension (2026-09-18, copilot gate round 1 B2) for the cutover's
#: credential move (data bind-mount source + moved upstream credential).
CANONICAL_PLACEHOLDERS: tuple[str, ...] = (
    "<REPO_DIR>",
    "<UV_BIN>",
    "<ENV_FILE>",
    "<PROXY_HOST>",
    "<LAN_IP>",
    "<APP_PORT>",
    "<LIVE_HOST>",
    "<CONTAINER_HOST_PORT>",
    "<HOME_HANSARD_DIR>",
    "<HOME_HANSARD_UPSTREAM_DIR>",
)

#: Forbidden machine-specific strings in deploy artifacts (belt-and-braces
#: alongside the publish-hygiene gate; 27.2-REQ-01). Mirrors the hygiene
#: gate's four canonical tokens exactly (test_gate_literals_match_hygiene_gate).
_FORBIDDEN_DEPLOY_STRINGS: tuple[str, ...] = (
    "/home/tyc",
    "192.168.8.",
    ".env.services",
    "pi@192.168.8.165",
)

#: The deploy files whose text must be machine-string-free.
_DEPLOY_TEMPLATE_FILES: tuple[Path, ...] = (
    _RUNBOOK,
    _SYSTEMD_DIR / "alice-hansard-gateway.service",
    _SYSTEMD_DIR / "alice-hansard-crawl.service",
    _SYSTEMD_DIR / "alice-hansard-crawl.timer",
    _DEPLOY_DIR / "caddy" / "hansard.098020.xyz.caddyfile",
)


def test_deploy_templates_carry_no_machine_strings() -> None:
    """No forbidden machine string survives in any deploy/ template file."""
    violations: list[str] = []
    for path in _DEPLOY_TEMPLATE_FILES:
        assert path.exists(), f"{path.name} missing"
        text = path.read_text(encoding="utf-8")
        for bad in _FORBIDDEN_DEPLOY_STRINGS:
            if bad in text:
                violations.append(f"{path.name}: {bad!r}")
    assert not violations, (
        "forbidden machine string(s) in deploy templates:\n" + "\n".join(violations)
    )


def test_gate_literals_match_hygiene_gate() -> None:
    """The publish-hygiene gate's forbidden tokens stay covered by the
    deploy-pins list.

    The hygiene gate (``_FORBIDDEN_MACHINE_STRINGS``) is the authoritative
    four-token list; its tokens must remain substrings of this file's
    prefix list (belt-and-braces per-file pins + repo-wide scan). A drift
    that shrinks either list silently widens the publish surface
    (27.2-REQ-01 / T-27.2-05).
    """
    import re

    hygiene = (REPO_ROOT / "tests" / "test_publish_hygiene.py").read_text(
        encoding="utf-8"
    )
    block = re.search(
        r"_FORBIDDEN_MACHINE_STRINGS: tuple\[str, \.\.\.\] = \((.*?)\)", hygiene, re.S
    )
    assert block, "hygiene gate _FORBIDDEN_MACHINE_STRINGS block not found"
    hygiene_tokens = re.findall(r'"([^"]+)"', block.group(1))
    for token in hygiene_tokens:
        assert any(token.startswith(prefix) or prefix in token for prefix in _FORBIDDEN_DEPLOY_STRINGS), (
            f"hygiene gate token {token!r} no longer covered by the deploy-pins list"
        )


def test_crawl_service_unit_exists() -> None:
    """The systemd --user oneshot crawl service file exists."""
    service = _SYSTEMD_DIR / "alice-hansard-crawl.service"
    assert service.exists(), "deploy/systemd/alice-hansard-crawl.service missing"
    text = service.read_text(encoding="utf-8")
    # A --user oneshot (no sudo required to install).
    assert "Type=oneshot" in text
    assert "[Install]" in text
    # WorkingDirectory + EnvironmentFile mirror the existing gateway unit
    # (placeholder forms since Phase 27.2 generalized the templates).
    assert "WorkingDirectory=<REPO_DIR>" in text
    assert "EnvironmentFile=<ENV_FILE>" in text
    # The weekly run is INCREMENTAL (no --cold — the cold crawl is a
    # one-off manual RUNBOOK step, not the timer's job).
    assert "python -m crawl.crawl_main" in text
    assert "--cold" not in text
    # Logs to the journal.
    assert "StandardOutput=journal" in text
    assert "StandardError=journal" in text


def test_crawl_timer_unit_exists() -> None:
    """The systemd --user crawl timer file exists with the required [Timer]."""
    timer = _SYSTEMD_DIR / "alice-hansard-crawl.timer"
    assert timer.exists(), "deploy/systemd/alice-hansard-crawl.timer missing"
    text = timer.read_text(encoding="utf-8")
    assert "[Timer]" in text
    # Weekly + persistent (a missed run fires on next boot — RESEARCH deploy
    # surface) + a randomised delay to spread the day-long crawl.
    assert "OnCalendar=weekly" in text
    assert "Persistent=true" in text
    assert "RandomizedDelaySec" in text
    assert "[Unit]" in text and "Description=" in text
    assert "[Install]" in text


def test_runbook_has_cold_crawl_section() -> None:
    """The RUNBOOK documents the cold crawl + the timer install + the index
    file verification."""
    assert _RUNBOOK.exists(), "deploy/RUNBOOK.md missing"
    text = _RUNBOOK.read_text(encoding="utf-8")
    # The Phase 27.1 section heading.
    assert "Term index crawl (Phase 27.1)" in text
    # The timer install commands.
    assert "systemctl --user enable --now alice-hansard-crawl.timer" in text
    assert "systemctl --user daemon-reload" in text
    # The COLD crawl step (one-off, BEFORE live acceptance).
    assert "crawl.crawl_main --cold" in text
    assert "one-off" in text.lower() or "ONE-OFF" in text
    # The index file verification (0600 file, 0700 dir).
    assert ".hansard/index.db" in text
    assert "0600" in text
    assert "0700" in text
    # The gateway restart after the cold crawl.
    assert "systemctl --user restart alice-hansard-gateway" in text


def test_gitignore_covers_index_db() -> None:
    """.gitignore lists the index DB (build artifact, never committed)."""
    assert _GITIGNORE.exists(), ".gitignore missing"
    text = _GITIGNORE.read_text(encoding="utf-8")
    for name in (_INDEX_DB_NAME, f"{_INDEX_DB_NAME}.staging", f"{_INDEX_DB_NAME}.old"):
        assert name in text, f".gitignore missing {name!r}"


def test_config_index_db_path_under_hansard() -> None:
    """config.index_db_path points under a .hansard dir and ends in index.db
    (the 0700 dir that holds the consumer tokens)."""
    settings = Settings()
    path = settings.index_db_path
    assert path.name == _INDEX_DB_NAME
    assert path.parent.name == ".hansard", (
        f"index_db_path parent is {path.parent.name!r}, expected '.hansard'"
    )
