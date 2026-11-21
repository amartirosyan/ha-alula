"""Quick follow-up refresh burst after a command. No Home Assistant needed."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "alula" / "fast_refresh.py"
_spec = importlib.util.spec_from_file_location("alula_fast_refresh", _PATH)
fast_refresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fast_refresh)  # type: ignore[union-attr]
FastRefresher = fast_refresh.FastRefresher


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


def test_default_schedule_is_quick_and_bounded() -> None:
    delays = fast_refresh.FAST_REFRESH_DELAYS
    assert delays[0] <= 5
    assert len(delays) <= 4
    assert all(b - a >= 5 for a, b in zip(delays, delays[1:]))


@pytest.mark.asyncio
async def test_burst_refreshes_on_schedule_until_state_changes() -> None:
    clock = _Clock()
    state = {"v": "armed_home"}
    times: list[float] = []

    async def refresh() -> None:
        times.append(clock.now)
        if len(times) == 2:
            state["v"] = "disarmed"

    r = FastRefresher(refresh, lambda: state["v"], delays=(3, 8, 15, 25), sleep=clock.sleep)
    done = await r.trigger()
    assert times == [3, 8]
    assert done == 2


@pytest.mark.asyncio
async def test_burst_runs_all_steps_when_state_never_changes() -> None:
    clock = _Clock()
    times: list[float] = []

    async def refresh() -> None:
        times.append(clock.now)

    r = FastRefresher(refresh, lambda: "armed_home", delays=(3, 8, 15, 25), sleep=clock.sleep)
    assert await r.trigger() == 4
    assert times == [3, 8, 15, 25]


@pytest.mark.asyncio
async def test_new_command_restarts_burst_without_overlap() -> None:
    gate = asyncio.Event()
    calls: list[str] = []

    async def slow_sleep(_: float) -> None:
        await gate.wait()

    async def refresh() -> None:
        calls.append("r")

    r = FastRefresher(refresh, lambda: "x", delays=(3,), sleep=slow_sleep)
    first = r.trigger()
    await asyncio.sleep(0)
    second = r.trigger()
    await asyncio.sleep(0)
    assert first.cancelled() or first.done()
    gate.set()
    assert await second == 1
    assert calls == ["r"]  # the replaced burst never refreshed


@pytest.mark.asyncio
async def test_refresh_errors_do_not_stop_the_burst() -> None:
    clock = _Clock()
    n = {"c": 0}

    async def refresh() -> None:
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("cloud busy")

    r = FastRefresher(refresh, lambda: "s", delays=(1, 6), sleep=clock.sleep)
    assert await r.trigger() == 2


# ---- DelayPoller ----------------------------------------------------------

DelayPoller = fast_refresh.DelayPoller


class _WallClock(_Clock):
    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_delay_poll_every_5s_with_final_check_and_stops_on_end() -> None:
    clock = _WallClock()
    info = {"v": (0.0, 12.0)}
    times: list[float] = []

    async def refresh() -> None:
        times.append(clock.now)
        if clock.now >= 12.0:
            info["v"] = None  # panel reports the delay over

    p = DelayPoller(refresh, lambda: info["v"], sleep=clock.sleep, clock=clock)
    assert await p.ensure() == 3
    assert times == [5.0, 10.0, 12.5]


@pytest.mark.asyncio
async def test_delay_poll_gives_up_after_timer_plus_grace() -> None:
    clock = _WallClock()
    times: list[float] = []

    async def refresh() -> None:
        times.append(clock.now)

    # Delay flag never clears (stale status): stop at timer + 15 s.
    p = DelayPoller(refresh, lambda: (0.0, 10.0), sleep=clock.sleep, clock=clock)
    polls = await p.ensure()
    assert times[-1] <= 25.0 + 1e-9
    assert 10.5 in times
    assert polls == len(times) and polls <= 8
    assert p.ensure() is None  # past the deadline: no new task


@pytest.mark.asyncio
async def test_delay_poll_not_started_without_delay_and_single_task() -> None:
    clock = _WallClock()
    p = DelayPoller(AsyncMock(), lambda: None, sleep=clock.sleep, clock=clock)
    assert p.ensure() is None
    gate = asyncio.Event()

    async def slow_sleep(_: float) -> None:
        await gate.wait()

    q = DelayPoller(AsyncMock(), lambda: (0.0, 60.0), sleep=slow_sleep, clock=clock)
    t1 = q.ensure()
    t2 = q.ensure()
    assert t1 is t2 and q.running
    q.cancel()
    await asyncio.sleep(0)
    assert not q.running
