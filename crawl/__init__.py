"""Standalone offline SPRS sitting-TOC crawl (Phase 27.1, spec §3.1; 27.5 loop).

NOT imported by the request path. Run via
``uv run python -m crawl.crawl_main`` — directly, on a host timer
(cron / systemd), or as the in-image ``crawl loop`` companion (Phase 27.5).
A failed crawl never touches the live index (staging DB + atomic
os.replace), and every write path takes the single-writer flock
(``crawl_db.index_lock``).
"""
