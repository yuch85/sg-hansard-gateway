"""Standalone offline SPRS sitting-TOC crawl (Phase 27.1, spec §3.1).

NOT imported by the request path. Run via
``uv run python -m crawl.crawl_main`` from a systemd --user timer; a failed
crawl never touches the live index (staging DB + atomic os.replace).
"""
