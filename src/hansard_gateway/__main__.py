"""Production entrypoint (systemd unit ExecStart).

Runs the app under uvicorn with structured request logging configured
FIRST — the uvicorn CLI (``uvicorn hansard_gateway.main:app``) imports the
app module but never calls :func:`configure_logging`, so a unit pointed at
``main:app`` gets a logger with no handlers and D-09 request logging is
silently dead (found 2026-09-18 during the /search arrival diagnosis).

Usage: ``uv run python -m hansard_gateway`` (see the deploy systemd unit), or
``uv run python -m hansard_gateway crawl once [--cold]`` to run an offline
crawl (27.2-REQ-03) without the server.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

#: Subcommand name for the server (27.2-REQ-03: explicit serve + crawl).
SERVE_COMMAND: str = "serve"
#: Subcommand name for the offline crawl dispatch.
CRAWL_COMMAND: str = "crawl"
#: The one crawl action supported from the gateway entrypoint.
CRAWL_ACTION_ONCE: str = "once"
#: Top-level flag that must stay accepted at BOTH levels (bare + serve).
_HOST_FLAG: str = "--host"
#: Top-level flag that must stay accepted at BOTH levels (bare + serve).
_PORT_FLAG: str = "--port"


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser: bare serve (default) + ``crawl once [--cold]``.

    Bare invocation keeps the exact pre-27.2 interface (--host/--port at the
    top level) so the systemd unit needs no change; ``serve`` accepts the same
    flags explicitly.
    """
    parser = argparse.ArgumentParser(description="Hansard Gateway server")
    parser.add_argument("--host", default="0.0.0.0", help="bind address")
    parser.add_argument(
        "--port", type=int, default=None, help="bind port (default: Settings.app_port)"
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser(SERVE_COMMAND, help="run the gateway server (default)")
    serve.add_argument("--host", default="0.0.0.0", help="bind address")
    serve.add_argument(
        "--port", type=int, default=None, help="bind port (default: Settings.app_port)"
    )

    crawl = sub.add_parser(CRAWL_COMMAND, help="run an offline index crawl")
    crawl_sub = crawl.add_subparsers(dest="action", required=True)
    once = crawl_sub.add_parser(CRAWL_ACTION_ONCE, help="crawl once and exit")
    once.add_argument(
        "--cold",
        action="store_true",
        help="full rebuild from crawl_start_date (one-off RUNBOOK step)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Parse args, dispatch to serve or crawl, and run to completion."""
    parser = _build_parser()

    # Under `python -m hansard_gateway` main() is called with argv=None while
    # the real CLI args live in sys.argv[1:] — fall back to those so the crawl
    # subcommand works from the module entrypoint (not just direct import).
    if argv is None:
        argv = sys.argv[1:]

    # Bare invocation (no subcommand) keeps the pre-27.2 interface: --host /
    # --port / -h at the top level. Rewrite to the explicit `serve` form; the
    # duplicated flags parse identically at either level.
    if argv and argv[0] not in (SERVE_COMMAND, CRAWL_COMMAND, "-h", "--help"):
        argv = [SERVE_COMMAND, *argv]
    elif not argv:
        argv = [SERVE_COMMAND]
    args = parser.parse_args(argv)

    if args.command == CRAWL_COMMAND:
        from crawl.crawl_main import main as crawl_cli

        return crawl_cli(["--cold"] if args.cold else [])

    import uvicorn

    from hansard_gateway.config import settings
    from hansard_gateway.logging_setup import configure_logging
    from hansard_gateway.main import create_app

    configure_logging(settings=settings)
    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port if args.port is not None else settings.app_port,
        access_log=False,  # D-10: the token is in the path; never log it
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
