"""Quick follow-up refreshes after a panel command.

The Alula cloud reports a new arm state some seconds after the panel acts,
and the normal poll is every 30 s. After a command, refresh a few times
soon, then stop. A new command restarts the burst (debounced), and the
burst stops as soon as the arm state differs from the state it started
with. Each refresh already takes about 5 s (REST plus a WebSocket zone
read), so the schedule never runs two at once and adds at most four calls
per command. No Home Assistant imports, so it is unit-testable.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Seconds after the command finished (the panel ACK or the end of the
# arm-then-bypass sequence) at which to refresh.
FAST_REFRESH_DELAYS: tuple[float, ...] = (3.0, 8.0, 15.0, 25.0)


class FastRefresher:
    """Run a short, debounced burst of refreshes."""

    def __init__(
        self,
        refresh: Callable[[], Awaitable[None]],
        current_state: Callable[[], Any],
        delays: tuple[float, ...] = FAST_REFRESH_DELAYS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._refresh = refresh
        self._state = current_state
        self._delays = tuple(sorted(delays))
        self._sleep = sleep
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def trigger(self) -> asyncio.Task:
        """Start a burst, replacing any burst already running."""
        if self.running:
            self._task.cancel()  # type: ignore[union-attr]
        baseline = self._safe_state()
        self._task = asyncio.get_running_loop().create_task(self._run(baseline))
        return self._task

    def cancel(self) -> None:
        if self.running:
            self._task.cancel()  # type: ignore[union-attr]
        self._task = None

    def _safe_state(self) -> Any:
        try:
            return self._state()
        except Exception:  # noqa: BLE001
            return None

    async def _run(self, baseline: Any) -> int:
        done = 0
        elapsed = 0.0
        for delay in self._delays:
            await self._sleep(max(delay - elapsed, 0))
            elapsed = delay
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Alula fast refresh failed: %s", err)
            done += 1
            state = self._safe_state()
            if state is not None and state != baseline:
                _LOGGER.info(
                    "Alula state changed to %s after %d quick refresh(es)", state, done
                )
                break
        return done


# While an exit or entry delay runs: poll every DELAY_POLL_INTERVAL seconds,
# check once right after the timer reaches 0, and give up DELAY_POLL_GRACE
# seconds after the timer would have ended.
DELAY_POLL_INTERVAL: float = 5.0
DELAY_POLL_GRACE: float = 15.0


class DelayPoller:
    """Poll while a panel delay runs; the panel sends no push when it ends.

    ``delay_info`` returns ``(started_at, timer_seconds)`` (wall clock) while
    the panel reports a delay, else ``None``. ``ensure()`` is cheap and safe to
    call after every update: it starts one polling task when a delay is
    running and none is active.
    """

    def __init__(
        self,
        refresh: Callable[[], Awaitable[None]],
        delay_info: Callable[[], tuple[float, float] | None],
        interval: float = DELAY_POLL_INTERVAL,
        grace: float = DELAY_POLL_GRACE,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._refresh = refresh
        self._info = delay_info
        self._interval = interval
        self._grace = grace
        self._sleep = sleep
        self._clock = clock
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def ensure(self) -> asyncio.Task | None:
        if self.running:
            return self._task
        info = self._safe_info()
        if info is None or self._clock() >= info[0] + info[1] + self._grace:
            return None
        self._task = asyncio.get_running_loop().create_task(self._run())
        return self._task

    def cancel(self) -> None:
        if self.running:
            self._task.cancel()  # type: ignore[union-attr]
        self._task = None

    def _safe_info(self) -> tuple[float, float] | None:
        try:
            return self._info()
        except Exception:  # noqa: BLE001
            return None

    async def _run(self) -> int:
        polls = 0
        info = self._safe_info()
        limit = (
            int(math.ceil((info[1] + self._grace) / self._interval)) + 3 if info else 0
        )
        while info is not None and polls < limit:
            started, timer = info
            now = self._clock()
            end = started + timer
            deadline = end + self._grace
            if now >= deadline:
                _LOGGER.debug("Alula delay poll stopped: past timer + grace")
                break
            left = end - now
            if left > self._interval:
                wait = self._interval
            elif left > 0:
                wait = left + 0.5  # final check just after the timer hits 0
            else:
                wait = min(self._interval, deadline - now)
            await self._sleep(wait)
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Alula delay poll failed: %s", err)
            polls += 1
            info = self._safe_info()
        if info is None:
            _LOGGER.debug("Alula delay ended after %d poll(s)", polls)
        return polls
