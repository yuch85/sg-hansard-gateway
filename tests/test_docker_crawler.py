"""Phase 27.5 plan 02: two-process container (tini + entrypoint) contracts.

Two offline groups, no docker build anywhere:
- source assertions on Dockerfile / .dockerignore / .env.example / compose;
- entrypoint behavior, proven by running the real docker-entrypoint.sh with a
  stubbed ``uv`` on PATH (the stub logs its argv, traps TERM, and sleeps).

The REAL-chain proof (tini -> bash -> uv run -> python signal delivery on the
built image) is Plan 03 T2's post-build verification, not this file.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# --- Group 1: source assertions (T1.1 - T1.5) ------------------------------


def test_dockerfile_installs_tini() -> None:
    dockerfile = (REPO / "Dockerfile").read_text()
    assert "apt-get install" in dockerfile and "tini" in dockerfile
    assert "--no-install-recommends" in dockerfile


def test_dockerfile_ships_entrypoint_and_example() -> None:
    dockerfile = (REPO / "Dockerfile").read_text()
    assert "docker-entrypoint.sh" in dockerfile, "entrypoint is copied into the image"
    assert ".env.example" in dockerfile, "the canonical env doc ships in the image"
    assert 'CMD ["tini", "--", "/app/docker-entrypoint.sh"]' in dockerfile
    instruction_lines = [
        l.strip()
        for l in dockerfile.splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    assert not any(l.startswith("ENTRYPOINT") for l in instruction_lines), (
        "no ENTRYPOINT instruction ever (CMD override contract)"
    )


def test_dockerignore_unexcludes_env_example() -> None:
    lines = (REPO / ".dockerignore").read_text().splitlines()
    assert ".env.*" in lines
    assert "!.env.example" in lines
    assert lines.index("!.env.example") > lines.index(".env.*")


def test_env_example_documents_crawler_flags() -> None:
    env_example = (REPO / ".env.example").read_text()
    for var in (
        "HANSARD_CRAWL_START_DATE",
        "HANSARD_CRAWL_RECRAWL_AGE_DAYS",
        "HANSARD_CRAWL_EMPTY_RECHECK_DAYS",
        "HANSARD_CRAWL_ENABLED",
        "HANSARD_CRAWL_INTERVAL_HOURS",
        "HANSARD_CRAWL_FULL_HISTORY",
        "HANSARD_CRAWL_FIRST_RUN_DAYS",
        # The entrypoint supervisor knob (27.5 plan 02 T2).
        "HANSARD_CRAWL_RESTART_SECONDS",
        # The six pre-existing hydrated vars.
        "HANSARD_APP_PORT",
        "HANSARD_INDEX_DB_PATH",
        "HANSARD_TOKENS_PATH",
        "HANSARD_PUBLIC_BASE_URL",
        "HANSARD_LOG_FILE",
        "HANSARD_ROBOTS_PATH",
        # A SEVENTH var read directly by logging_setup.py via os.environ —
        # pinned so the shipped file never loses it (checker L5).
        "HANSARD_LOG_STREAM",
    ):
        assert var in env_example, var


def test_compose_example_offers_crawler_flags() -> None:
    compose = (REPO / "examples" / "caddy" / "docker-compose.yml").read_text()
    assert "HANSARD_CRAWL_ENABLED" in compose


# --- Group 2: entrypoint behavior with a stubbed uv (T1.6 - T1.12) ----------

_STUB = """#!/usr/bin/env bash
# Stub uv: log argv, trap TERM, sleep forever. STUB_LOOP_CRASH=1 makes the
# crawl-loop invocation exit 1 immediately; STUB_SERVER_EXIT=<n> makes the
# serve invocation exit <n> immediately.
echo "START $$ $*" >> "$STUB_LOG"
if [[ "$*" == *"crawl loop"* && -n "${STUB_LOOP_CRASH:-}" ]]; then
  echo "CRASH $$" >> "$STUB_LOG"
  exit 1
fi
if [[ "$*" == *"serve"* && -n "${STUB_SERVER_EXIT:-}" ]]; then
  echo "SERVEREXIT $$" >> "$STUB_LOG"
  # Deterministic ordering: let the crawl loop start before the server dies.
  for _ in $(seq 1 100); do
    grep -q "crawl loop" "$STUB_LOG" 2>/dev/null && break
    sleep 0.1
  done
  exit "$STUB_SERVER_EXIT"
fi
trap 'kill $! 2>/dev/null; echo "TERM $$ $*" >> "$STUB_LOG"; exit 0' TERM INT
sleep 1000 &
wait $!
"""


@pytest.fixture()
def stub_env(tmp_path: Path) -> dict:
    """A PATH with a stubbed uv + a fresh stub log; returns the env dict."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    log = tmp_path / "stub.log"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    env["STUB_LOG"] = str(log)
    # Strip any ambient crawler env so defaults are exercised.
    for var in (
        "HANSARD_CRAWL_ENABLED",
        "HANSARD_CRAWL_RESTART_SECONDS",
        "HANSARD_CRAWL_INTERVAL_HOURS",
        "HANSARD_CRAWL_FULL_HISTORY",
        "HANSARD_CRAWL_FIRST_RUN_DAYS",
        "HANSARD_APP_PORT",
    ):
        env.pop(var, None)
    return env


def _log_lines(env: dict) -> list[str]:
    log = Path(env["STUB_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def _start(env: dict, **extra: str) -> subprocess.Popen:
    full_env = {**env, **extra}
    return subprocess.Popen(
        ["bash", str(REPO / "docker-entrypoint.sh")],
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_entrypoint_starts_server_only_by_default(stub_env: dict) -> None:
    proc = _start(stub_env)
    try:
        assert _wait_for(lambda: any("serve" in l for l in _log_lines(stub_env)))
        time.sleep(1.0)  # a beat to prove no loop ever starts
        lines = _log_lines(stub_env)
        assert any("serve" in l for l in lines)
        assert not any("crawl loop" in l for l in lines), "off by default: never crawls"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


def test_entrypoint_starts_loop_when_enabled(stub_env: dict) -> None:
    proc = _start(stub_env, HANSARD_CRAWL_ENABLED="1")
    try:
        assert _wait_for(
            lambda: any("serve" in l for l in _log_lines(stub_env))
            and any("crawl loop" in l for l in _log_lines(stub_env))
        )
        lines = _log_lines(stub_env)
        assert any("serve" in l for l in lines)
        assert any("crawl loop" in l for l in lines)
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.parametrize("word", [" yes ", "TRUE", " 1", "YeS\t"])
def test_entrypoint_enabled_strips_whitespace_and_true_words(stub_env: dict, word: str) -> None:
    """Review fold R1.1: the enable rule matches config.py _env_bool —
    leading/trailing whitespace is stripped and 1/true/yes (any case) enable."""
    proc = _start(stub_env, HANSARD_CRAWL_ENABLED=word)
    try:
        assert _wait_for(
            lambda: any("crawl loop" in l for l in _log_lines(stub_env))
        ), f"HANSARD_CRAWL_ENABLED={word!r} must enable the loop"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.parametrize("word", ["yes", " true ", "1"])
def test_entrypoint_full_history_true_words_suppress_fresh_warning(stub_env: dict, word: str) -> None:
    """Review fold R1.2: the fresh-volume warning honours the same true-word
    set for HANSARD_CRAWL_FULL_HISTORY, not only the literal 1."""
    proc = _start(
        stub_env,
        HANSARD_CRAWL_ENABLED="1",
        HANSARD_CRAWL_FULL_HISTORY=word,
        HANSARD_INDEX_DB_PATH="/nonexistent-fresh-volume/index.db",
    )
    try:
        assert _wait_for(lambda: any("crawl loop" in l for l in _log_lines(stub_env)))
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
        assert "fresh index" not in out, out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_entrypoint_fresh_warning_shown_without_full_history(stub_env: dict) -> None:
    """Control for the fold above: a fresh volume WITHOUT full history warns."""
    proc = _start(
        stub_env,
        HANSARD_CRAWL_ENABLED="1",
        HANSARD_INDEX_DB_PATH="/nonexistent-fresh-volume/index.db",
    )
    try:
        assert _wait_for(lambda: any("crawl loop" in l for l in _log_lines(stub_env)))
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
        assert "fresh index" in out, out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_entrypoint_startup_log_states_crawler_on(stub_env: dict) -> None:
    proc = _start(
        stub_env,
        HANSARD_CRAWL_ENABLED="1",
        HANSARD_CRAWL_INTERVAL_HOURS="12",
        HANSARD_CRAWL_FIRST_RUN_DAYS="31",
    )
    try:
        assert _wait_for(lambda: any("crawl loop" in l for l in _log_lines(stub_env)))
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
        assert "HANSARD_CRAWL_ENABLED=1" in out or "crawler ON" in out
        assert "12" in out, "startup log states the interval"
        assert "first-run" in out or "guard" in out, "startup log states the guard state"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_entrypoint_forwards_sigterm(stub_env: dict) -> None:
    proc = _start(stub_env, HANSARD_CRAWL_ENABLED="1")
    assert _wait_for(
        lambda: any("serve" in l for l in _log_lines(stub_env))
        and any("crawl loop" in l for l in _log_lines(stub_env))
    )
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=10)
    assert rc == 0, "clean signal shutdown exits 0"
    lines = _log_lines(stub_env)
    assert any(l.startswith("TERM") and "serve" in l for l in lines), "server got TERM"
    assert any(l.startswith("TERM") and "crawl loop" in l for l in lines), "loop got TERM"


def test_entrypoint_restarts_crashed_loop_only(stub_env: dict) -> None:
    proc = _start(
        stub_env,
        HANSARD_CRAWL_ENABLED="1",
        HANSARD_CRAWL_RESTART_SECONDS="1",
        STUB_LOOP_CRASH="1",
    )
    try:
        assert _wait_for(
            lambda: len([l for l in _log_lines(stub_env) if "crawl loop" in l and l.startswith("START")]) >= 2,
            timeout=15.0,
        )
        lines = _log_lines(stub_env)
        serves = [l for l in lines if l.startswith("START") and "serve" in l]
        assert len(serves) == 1, "the server process was never restarted"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


def test_entrypoint_server_exit_stops_container(stub_env: dict) -> None:
    proc = _start(stub_env, HANSARD_CRAWL_ENABLED="1", STUB_SERVER_EXIT="42")
    rc = proc.wait(timeout=15)
    assert rc == 42, "the script exits with the server's code"
    assert _wait_for(
        lambda: any(l.startswith("TERM") and "crawl loop" in l for l in _log_lines(stub_env)),
        timeout=5.0,
    ), "the loop side was stopped when the server exited"


@pytest.mark.parametrize("bad", ["abc", "0", "-5"])
def test_entrypoint_validates_restart_seconds(stub_env: dict, bad: str) -> None:
    """Defect 3: non-numeric/non-positive restart delay warns and falls back to 30."""
    proc = _start(
        stub_env,
        HANSARD_CRAWL_ENABLED="1",
        HANSARD_CRAWL_RESTART_SECONDS=bad,
        STUB_LOOP_CRASH="1",
    )
    try:
        assert _wait_for(lambda: any("crawl loop" in l for l in _log_lines(stub_env)))
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
        assert "HANSARD_CRAWL_RESTART_SECONDS" in out and "30" in out, out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_entrypoint_binds_8000_regardless_of_app_port(stub_env: dict) -> None:
    """Defect 2: HANSARD_APP_PORT is a host-run knob; the container server
    always binds 8000 (the HEALTHCHECK probes hardcoded 127.0.0.1:8000)."""
    proc = _start(stub_env, HANSARD_APP_PORT="9999")
    try:
        assert _wait_for(lambda: any("serve" in l for l in _log_lines(stub_env)))
        lines = _log_lines(stub_env)
        serve = next(l for l in lines if "serve" in l)
        assert "--port 8000" in serve
        assert "9999" not in serve
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
