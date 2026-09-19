"""27.2-REQ-07: the ``hg-tokens`` CLI (generate/rotate/list/disable).

Drives ``hansard_gateway.manage_tokens.main`` with argv lists against a tmp
HANSARD_TOKENS_PATH store — no network, no real store. The import target is the
package module the ``[project.scripts] hg-tokens`` entrypoint resolves to, so
the test passes in a fresh clone (it never imports the repo-root shim).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hansard_gateway.manage_tokens import EXIT_LABEL_ERROR, EXIT_STORE_ERROR, main


@pytest.fixture()
def token_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a fresh tmp store (HANSARD_TOKENS_PATH)."""
    store = tmp_path / "tokens.yaml"
    monkeypatch.setenv("HANSARD_TOKENS_PATH", str(store))
    return store


def test_generate_prints_plaintext_once_with_banner(
    token_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """generate <label> exits 0 and prints the hg_ plaintext exactly once."""
    rc = main(["generate", "team-test"])
    out = capsys.readouterr().out

    assert rc == 0
    assert out.count("hg_") == 1, f"plaintext must print exactly once, got: {out!r}"
    assert "SAVE THIS VALUE NOW." in out
    assert "team-test" in out


def test_generate_persists_digest_never_plaintext(
    token_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """generate persists a store with the label + sha256 — never the plaintext."""
    main(["generate", "team-test"])
    capsys.readouterr()

    assert token_env.exists()
    text = token_env.read_text(encoding="utf-8")
    assert "team-test" in text
    assert "sha256" in text
    assert "hg_" not in text  # only the digest is stored


def test_list_shows_label_and_last4_never_plaintext(
    token_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """list shows label + enabled + last4; never the plaintext token."""
    main(["generate", "team-test"])
    capsys.readouterr()  # discard generate output

    rc = main(["list"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "team-test" in out
    assert "yes" in out  # enabled
    assert "hg_" not in out  # plaintext never leaks into list


def test_rotate_prints_new_plaintext_keeps_label(
    token_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """rotate <label> prints a NEW plaintext; list still shows the same label."""
    main(["generate", "team-test"])
    capsys.readouterr()

    rc = main(["rotate", "team-test"])
    rotate_out = capsys.readouterr().out
    assert rc == 0
    assert "hg_" in rotate_out

    main(["list"])
    list_out = capsys.readouterr().out
    assert "team-test" in list_out


def test_disable_sets_enabled_false(
    token_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """disable <label> flips enabled to false (list shows 'no')."""
    main(["generate", "team-test"])
    capsys.readouterr()

    rc = main(["disable", "team-test"])
    capsys.readouterr()
    assert rc == 0

    main(["list"])
    out = capsys.readouterr().out
    assert "team-test" in out
    assert "no" in out  # enabled: no


def test_missing_store_is_empty_not_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """list on a missing store: load_store returns [] -> '(no tokens)', exit 0."""
    monkeypatch.setenv("HANSARD_TOKENS_PATH", str(tmp_path / "absent.yaml"))
    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(no tokens)" in out


def test_malformed_store_fails_closed(
    token_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed store raises TokenStoreError -> exit code 2 (fail closed)."""
    token_env.write_text("tokens: [not a dict]\n", encoding="utf-8")
    rc = main(["list"])
    capsys.readouterr()
    assert rc == EXIT_STORE_ERROR


def test_missing_label_exits_label_error(
    token_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """disable/rotate on a missing label, or a duplicate generate, -> exit 3."""
    main(["generate", "team-a"])
    capsys.readouterr()

    assert main(["disable", "nope"]) == EXIT_LABEL_ERROR
    assert main(["rotate", "nope"]) == EXIT_LABEL_ERROR
    assert main(["generate", "team-a"]) == EXIT_LABEL_ERROR  # duplicate label
