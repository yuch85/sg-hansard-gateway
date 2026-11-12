"""Long-running in-image crawl companion (Phase 27.5, decisions S2/S3).

Runs the crawl immediately and then every ``crawl_interval_hours`` forever;
exits only on SIGTERM/SIGINT, with code 0. A failed run backs off (60s
doubling per consecutive failure, capped at 3600s) and NEVER exits the loop
nonzero; a raised exception is logged with its traceback and counted exactly
like a failed run (F3). A lock-skip (rc 3: another crawl holds the
single-writer lock) retries after 900s without accruing failure backoff.

Started by the image entrypoint only when ``HANSARD_CRAWL_ENABLED=1``; a bare
``docker run`` never crawls (S3).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from types import FrameType
from typing import Callable, Optional

from crawl.crawl_main import run_crawl
from hansard_gateway.config import Settings, settings as _settings

logger = logging.getLogger(__name__)

#: Retry delay after a lock-skip (rc 3): the other writer is still working,
#: and a lock skip accrues no failure backoff.
LOCK_SKIP_RETRY_S = 900
#: Failure backoff: base seconds, doubling per consecutive failure, capped.
BACKOFF_BASE_S = 60
BACKOFF_CAP_S = 3600


def _install_signal_handlers(stop: threading.Event) -> dict[int, object]:
    """Set ``stop`` on SIGTERM/SIGINT; return the previous handlers."""

    def _handler(signum: int, frame: Optional[FrameType]) -> None:
        logger.info("crawl loop received signal %d — shutting down", signum)
        stop.set()

    previous: dict[int, object] = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, _handler)
    return previous


def _restore_signal_handlers(previous: dict[int, object]) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler)  # type: ignore[arg-type]


def _sleep(
    sleep_fn: Callable[[float], None], seconds: float, stop: threading.Event
) -> None:
    """Sleep ``seconds``; return early once ``stop`` is set.

    A custom ``sleep_fn`` (tests) is called once with the full duration. The
    default ``time.sleep`` is chunked in <=1s steps so a signal interrupts
    the wait promptly.
    """
    if sleep_fn is not time.sleep:
        sleep_fn(seconds)
        return
    remaining = float(seconds)
    while remaining > 0 and not stop.is_set():
        step = min(remaining, 1.0)
        sleep_fn(step)
        remaining -= step


def run_loop(
    *,
    settings: Optional[Settings] = None,
    once_fn: Callable[..., int] = run_crawl,
    sleep_fn: Callable[[float], None] = time.sleep,
    stop_event: Optional[threading.Event] = None,
) -> int:
    """Tick immediately, then per schedule, until signalled; always exit 0.

    ``once_fn`` runs one crawl (default :func:`crawl.crawl_main.run_crawl`).
    A caller-provided ``stop_event`` replaces signal handling (tests);
    otherwise SIGTERM/SIGINT set the internal stop flag.
    """
    cfg = settings or _settings
    stop = stop_event if stop_event is not None else threading.Event()
    previous: Optional[dict[int, object]] = None
    if stop_event is None:
        try:
            previous = _install_signal_handlers(stop)
        except ValueError:
            logger.warning(
                "crawl loop: not on the main thread — signal handling disabled"
            )
    logger.info(
        "crawl loop starting: enabled=%s interval=%sh full_history=%s "
        "first_run_days=%s index=%s",
        cfg.crawl_enabled,
        cfg.crawl_interval_hours,
        cfg.crawl_full_history,
        cfg.crawl_first_run_days,
        cfg.index_db_path,
    )
    failures = 0
    try:
        while not stop.is_set():
            try:
                rc: Optional[int] = once_fn(settings=cfg)
            except Exception:
                rc = None
                failures += 1
                logger.exception(
                    "crawl tick raised; consecutive failures=%d", failures
                )
                wait = min(BACKOFF_BASE_S * (2 ** (failures - 1)), BACKOFF_CAP_S)
            else:
                if rc == 0:
                    failures = 0
                    wait = cfg.crawl_interval_hours * 3600
                elif rc == 3:
                    wait = float(LOCK_SKIP_RETRY_S)
                else:
                    failures += 1
                    logger.error(
                        "crawl tick failed rc=%d; consecutive failures=%d",
                        rc,
                        failures,
                    )
                    wait = min(BACKOFF_BASE_S * (2 ** (failures - 1)), BACKOFF_CAP_S)
            _sleep(sleep_fn, wait, stop)
    finally:
        if previous is not None:
            _restore_signal_handlers(previous)
    logger.info("crawl loop stopped cleanly (signal); exit 0")
    return 0
