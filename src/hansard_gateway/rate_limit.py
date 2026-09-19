"""In-process rate limiting + global upstream concurrency (D-12, addendum §16/§17).

Hand-rolled: per-token sliding windows (per-minute and per-day, from Settings),
a per-token in-flight cap, and a single global asyncio.Semaphore. The clock is
injectable so tests advance time deterministically. Concurrency justification
(STYLE.md): the gateway is a single-process async uvicorn worker, so an
asyncio.Lock serializes window mutations and one Semaphore bounds total upstream
fan-out — no cross-process coordination is needed.

No cross-token disclosure: an AdmissionResult never names another token's state.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Protocol

from hansard_gateway.config import Settings


class Clock(Protocol):
    """Monotonic clock abstraction (injectable for tests)."""

    def now(self) -> float:
        """Return the current monotonic time in seconds."""


class SystemClock:
    """Production clock backed by the running event loop's monotonic time."""

    def now(self) -> float:
        return asyncio.get_event_loop().time()


@dataclass(frozen=True)
class AdmissionResult:
    """Outcome of an admission check. `reason` is opaque to callers."""

    admitted: bool
    status: int
    reason: str


_ADMISSION_OK = AdmissionResult(admitted=True, status=200, reason="ok")
_PER_MIN_DENIED = AdmissionResult(admitted=False, status=429, reason="per_min")
_PER_DAY_DENIED = AdmissionResult(admitted=False, status=429, reason="per_day")
_INFLIGHT_DENIED = AdmissionResult(admitted=False, status=429, reason="in_flight")
_GLOBAL_SATURATED = AdmissionResult(admitted=False, status=503, reason="global")


class RateGate:
    """Per-label rate + concurrency gate with a global upstream semaphore."""

    def __init__(
        self, *, settings: Settings, clock: Clock | None = None
    ) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()
        self._minute_windows: Dict[str, Deque[float]] = defaultdict(deque)
        self._day_windows: Dict[str, Deque[float]] = defaultdict(deque)
        self._in_flight: Dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()
        self._global_sem = asyncio.Semaphore(settings.global_upstream_concurrency)

    async def admit(self, *, token_label: str) -> AdmissionResult:
        """Check the per-token rate windows; record the request if admitted."""
        now = self._clock.now()
        async with self._lock:
            self._prune(self._minute_windows[token_label], now, self._settings.minute_window_s)
            self._prune(self._day_windows[token_label], now, self._settings.day_window_s)
            if len(self._minute_windows[token_label]) >= self._settings.per_token_rate_per_min:
                return _PER_MIN_DENIED
            if len(self._day_windows[token_label]) >= self._settings.per_token_rate_per_day:
                return _PER_DAY_DENIED
            self._minute_windows[token_label].append(now)
            self._day_windows[token_label].append(now)
        return _ADMISSION_OK

    def _prune(self, window: Deque[float], now: float, span: int) -> None:
        """Drop timestamps older than the sliding window (caller holds lock)."""
        cutoff = now - span
        while window and window[0] <= cutoff:
            window.popleft()

    def acquire_upstream(self, *, token_label: str) -> "_UpstreamSlot":
        """Return an async context manager bounding per-token + global fan-out."""
        return _UpstreamSlot(gate=self, token_label=token_label)


class _UpstreamSlot:
    """Holds one global permit + one per-token in-flight slot for its lifetime."""

    def __init__(self, *, gate: RateGate, token_label: str) -> None:
        self._gate = gate
        self._label = token_label
        self._global_held = False

    async def __aenter__(self) -> AdmissionResult:
        gate = self._gate
        # Per-token in-flight cap first (cheap, no global side effect).
        async with gate._lock:
            if gate._in_flight[self._label] >= gate._settings.per_token_concurrent_upstream:
                return _INFLIGHT_DENIED
            gate._in_flight[self._label] += 1
        # Global saturation check (fast path): refuse rather than queue when
        # every global permit is already held (addendum §17 → 503).
        if gate._global_sem.locked():
            async with gate._lock:
                gate._in_flight[self._label] -= 1
            return _GLOBAL_SATURATED
        try:
            await gate._global_sem.acquire()
        except BaseException:
            async with gate._lock:
                gate._in_flight[self._label] -= 1
            raise
        self._global_held = True
        return _ADMISSION_OK

    async def __aexit__(self, *exc_info: object) -> None:
        gate = self._gate
        if self._global_held:
            gate._global_sem.release()
        async with gate._lock:
            gate._in_flight[self._label] -= 1
