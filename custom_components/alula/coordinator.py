"""DataUpdateCoordinator for the Alula integration."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

import asyncio

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .fast_refresh import DelayPoller, FastRefresher
from .pyalula import AlarmPanel, AlulaClient
from .pyalula.client import AlulaApiError, AlulaAuthError

_LOGGER = logging.getLogger(__name__)

# Seconds to wait for the panelStatus reply during a delay refresh.
DELAY_STATUS_WAIT = 1.5


class AlulaCoordinator(DataUpdateCoordinator[AlarmPanel]):
    """Polls the Alula REST API and refreshes zone state via WebSocket."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AlulaClient,
        panel_id: str | None = None,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self._client = client
        self._panel_id = panel_id
        self._client.on_arm_state_change = self._on_arm_state_change
        # One update at a time: the normal poll, a requested refresh and a
        # quick follow-up refresh must not overlap on the WebSocket.
        self._update_lock = asyncio.Lock()
        self._fast = FastRefresher(self.async_refresh, self._arm_state)
        # While an exit/entry delay runs, poll panelStatus + REST every ~5 s
        # (the panel does not push when the delay ends).
        self._client.on_panel_status = self._on_panel_status
        self._delay = DelayPoller(self._async_delay_refresh, self._delay_info)

    def _delay_info(self) -> tuple[float, float] | None:
        status = self._client.panel_status
        if status is None or not status.in_delay or status.delay_timer <= 0:
            return None
        return (status.started_at, float(status.delay_timer))

    @callback
    def async_update_listeners(self) -> None:
        super().async_update_listeners()
        self._delay.ensure()

    @callback
    def _on_panel_status(self) -> None:
        """New panelStatus version pushed: re-derive state now, then refresh."""
        data = self.data
        if data is not None:
            data.apply_status(self._client.panel_status, time.time())
            self.async_set_updated_data(data)
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_delay_refresh(self) -> None:
        """Light refresh during a delay: panelStatus + REST, zones kept."""
        async with self._update_lock:
            try:
                await self._client.request_panel_status()
                await asyncio.sleep(DELAY_STATUS_WAIT)
                panel = await self._client.get_panel_status(panel_id=self._panel_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Alula delay refresh failed: %s", err)
                return
        self.async_set_updated_data(panel)

    def _arm_state(self):
        data = self.data
        return getattr(data, "arm_state", None) if data is not None else None

    @callback
    def async_burst_refresh(self) -> None:
        """Refresh a few times soon after a command (debounced)."""
        self._fast.trigger()

    async def async_shutdown(self) -> None:
        self._fast.cancel()
        self._delay.cancel()
        await super().async_shutdown()

    def _on_arm_state_change(self) -> None:
        """Called by the client when a keypad push indicates an arm state transition."""
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_update_data(self) -> AlarmPanel:
        """Fetch fresh panel + zone data."""
        async with self._update_lock:
            return await self._async_fetch()

    async def _async_fetch(self) -> AlarmPanel:
        try:
            # Poll arm state via REST (also caches panel ID needed for WS)
            panel = await self._client.get_panel_status(panel_id=self._panel_id)

            # Ensure WebSocket is up (zone state + arm/disarm commands)
            if not self._client.ws_connected:
                await self._client.connect_ws()

            # Ask for panelStatus (exit/entry delay); the reply lands while
            # the zone read below waits for its responses.
            await self._client.request_panel_status(device_id=panel.id)

            # Refresh zone state via WebSocket
            zones = await self._client.fetch_zone_statuses(device_id=panel.id)
            panel.zones = zones
            panel.apply_status(self._client.panel_status, time.time())

            return panel

        except AlulaAuthError as err:
            raise UpdateFailed(f"Alula authentication failed: {err}") from err
        except AlulaApiError as err:
            raise UpdateFailed(f"Alula API error: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err
