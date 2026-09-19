"""Rate-gate tests: per-minute, per-day, in-flight, global, no cross-token leak.

`asyncio_mode = "auto"` (pyproject) lets these be plain `async def` tests that
`await` the gate directly.
"""

from __future__ import annotations

import pytest

from hansard_gateway.config import Settings
from hansard_gateway.rate_limit import RateGate

from tests.conftest import FakeClock


@pytest.fixture()
def gate() -> RateGate:
    return RateGate(settings=Settings(), clock=FakeClock())


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def gate_with_clock(clock: FakeClock) -> RateGate:
    return RateGate(settings=Settings(), clock=clock)


async def test_121st_request_in_minute_is_429(gate: RateGate) -> None:
    """The (limit+1)th per-minute request for a label returns the 429 signal."""
    limit = Settings().per_token_rate_per_min
    results = [await gate.admit(token_label="team-1") for _ in range(limit + 1)]
    assert all(r.admitted for r in results[:limit])
    assert results[limit].admitted is False
    assert results[limit].status == 429


async def test_minute_window_slides(
    clock: FakeClock, gate_with_clock: RateGate
) -> None:
    """After the 60s window slides, the label is admitted again."""
    for _ in range(Settings().per_token_rate_per_min):
        await gate_with_clock.admit(token_label="team-1")
    denied = await gate_with_clock.admit(token_label="team-1")
    assert denied.admitted is False
    clock.advance(61)
    admitted = await gate_with_clock.admit(token_label="team-1")
    assert admitted.admitted is True


async def test_per_token_inflight_sixth_concurrent_is_429(gate: RateGate) -> None:
    """The 6th concurrent upstream for a label returns 429."""
    slots = [gate.acquire_upstream(token_label="team-1") for _ in range(6)]
    results = [await s.__aenter__() for s in slots]
    for s in slots:
        await s.__aexit__(None, None, None)
    assert all(r.admitted for r in results[:5])
    assert results[5].admitted is False
    assert results[5].status == 429
    assert results[5].reason == "in_flight"


async def test_global_sixteenth_acquire_is_503(gate: RateGate) -> None:
    """The 16th global upstream acquire (across labels) returns 503."""
    labels = ["a", "b", "c"]
    slots = [gate.acquire_upstream(token_label=labels[i % 3]) for i in range(15)]
    results = [await s.__aenter__() for s in slots]
    assert all(r.admitted for r in results)
    # 16th acquire from a fresh label hits the saturated global semaphore.
    extra = gate.acquire_upstream(token_label="d")
    extra_result = await extra.__aenter__()
    for s in slots:
        await s.__aexit__(None, None, None)
    if extra_result.admitted:
        await extra.__aexit__(None, None, None)
    assert extra_result.admitted is False
    assert extra_result.status == 503
    assert extra_result.reason == "global"


async def test_no_cross_token_disclosure(gate: RateGate) -> None:
    """A second label is unaffected by the first label's 429."""
    for _ in range(Settings().per_token_rate_per_min):
        await gate.admit(token_label="team-1")
    first_denied = await gate.admit(token_label="team-1")
    second_ok = await gate.admit(token_label="team-2")
    assert first_denied.admitted is False
    assert first_denied.status == 429
    assert second_ok.admitted is True
    assert second_ok.status == 200
    # The denial reason never names another token's state.
    assert "team-2" not in first_denied.reason
