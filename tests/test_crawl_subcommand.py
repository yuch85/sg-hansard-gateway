"""27.2-REQ-03: ``python -m hansard_gateway crawl once [--cold]`` dispatch.

The crawl subcommand routes to the existing ``crawl.crawl_main`` without
touching crawl logic (run_crawl stubbed). The serve path is a separate branch
that never reaches the crawl client.
"""

from __future__ import annotations

from typing import Any

import pytest


class _CrawlRecorder:
    """Records the kwargs of each run_crawl call and returns a fixed code."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> int:
        self.calls.append(kwargs)
        return 0


@pytest.fixture()
def crawl_recorder(monkeypatch: pytest.MonkeyPatch) -> _CrawlRecorder:
    """Stub crawl.crawl_main.run_crawl with a recorder (no network, no sleep)."""
    import crawl.crawl_main as crawl_main

    rec = _CrawlRecorder()
    monkeypatch.setattr(crawl_main, "run_crawl", rec)
    return rec


def test_crawl_once_defaults_cold_false(crawl_recorder: _CrawlRecorder) -> None:
    """``crawl once`` dispatches to crawl_main with cold=False."""
    from hansard_gateway.__main__ import main

    rc = main(["crawl", "once"])

    assert rc == 0
    assert crawl_recorder.calls == [{"cold": False}]


def test_crawl_once_cold_flag(crawl_recorder: _CrawlRecorder) -> None:
    """``crawl once --cold`` dispatches to crawl_main with cold=True."""
    from hansard_gateway.__main__ import main

    rc = main(["crawl", "once", "--cold"])

    assert rc == 0
    assert crawl_recorder.calls == [{"cold": True}]


def test_crawl_parser_routes_once_to_crawl_subparser() -> None:
    """The parser maps ``crawl once [--cold]`` to the crawl branch (cold flag).

    Asserts the dispatch structure without booting uvicorn: the serve branch is
    only taken when the command is serve/absent, so a parsed ``crawl`` command
    proves the subcommand routing.
    """
    from hansard_gateway.__main__ import _build_parser

    assert _build_parser().parse_args(["crawl", "once"]).command == "crawl"
    assert _build_parser().parse_args(["crawl", "once"]).cold is False
    assert _build_parser().parse_args(["crawl", "once", "--cold"]).cold is True
    # Bare / explicit serve both land on the serve branch.
    assert _build_parser().parse_args(["serve"]).command == "serve"
    assert _build_parser().parse_args([]).command is None  # rewritten to serve


def test_crawl_loop_subcommand_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """27.5: ``crawl loop`` routes to crawl.crawl_loop.run_loop (stubbed).

    The loop subparser defines no --cold/--seed-state flags, so this also
    covers the F5 dispatch path (no AttributeError on the loop branch).
    """
    import crawl.crawl_loop as crawl_loop

    calls: list[dict[str, Any]] = []

    def fake_run_loop(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(crawl_loop, "run_loop", fake_run_loop)

    from hansard_gateway.__main__ import main

    assert main(["crawl", "loop"]) == 0
    assert len(calls) == 1

    # The crawl module's own CLI parses the action too.
    import crawl.crawl_main as crawl_main

    calls.clear()
    assert crawl_main.main(["loop"]) == 0
    assert len(calls) == 1


def test_crawl_parser_routes_loop_action() -> None:
    """27.5: the parser maps ``crawl loop`` to the loop action (no flags)."""
    from hansard_gateway.__main__ import _build_parser

    args = _build_parser().parse_args(["crawl", "loop"])
    assert args.command == "crawl"
    assert args.action == "loop"
