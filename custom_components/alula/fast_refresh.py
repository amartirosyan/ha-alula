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
