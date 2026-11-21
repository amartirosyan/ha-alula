"""Quick follow-up refresh burst after a command. No Home Assistant needed."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

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
