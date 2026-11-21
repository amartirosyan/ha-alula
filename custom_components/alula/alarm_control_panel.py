"""Alarm control panel entity for Alula."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_NIGHT_ARM_VARIANT,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DEFAULT_NIGHT_ARM_VARIANT,
    DOMAIN,
    FORCE_ARM_VARIANTS,
)
from .coordinator import AlulaCoordinator
from .identifiers import panel_unique_id
from .pyalula import AlarmPanel, AlulaClient, ArmState
from .pyalula.client import AlulaApiError, AlulaAuthError
from .pyalula.const import PROTEST_ANSWER_VARIANTS
from .safety import life_safety_zone_indexes

_LOGGER = logging.getLogger(__name__)

# Map Alula ArmState → HA AlarmControlPanelState
_STATE_MAP: dict[ArmState, AlarmControlPanelState] = {
    ArmState.DISARMED: AlarmControlPanelState.DISARMED,
    ArmState.ARMED_AWAY: AlarmControlPanelState.ARMED_AWAY,
    ArmState.ARMED_HOME: AlarmControlPanelState.ARMED_HOME,
    ArmState.ARMED_NIGHT: AlarmControlPanelState.ARMED_NIGHT,
    ArmState.TRIGGERED: AlarmControlPanelState.TRIGGERED,
    ArmState.ARMING: AlarmControlPanelState.ARMING,
    ArmState.PENDING: AlarmControlPanelState.PENDING,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Alula alarm control panel."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: AlulaCoordinator = data[DATA_COORDINATOR]
    client: AlulaClient = data[DATA_CLIENT]
    async_add_entities([AlulaAlarmPanel(coordinator, client, entry)])


class AlulaAlarmPanel(CoordinatorEntity[AlulaCoordinator], AlarmControlPanelEntity):
    """Representation of the Alula alarm panel."""

    _attr_has_entity_name = True
    _attr_name = None  # use device name as entity name
    _attr_code_format = CodeFormat.NUMBER
    _attr_code_arm_required = False  # set True if API requires code to arm
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_NIGHT
        | AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
    )

    def __init__(
        self,
        coordinator: AlulaCoordinator,
        client: AlulaClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._entry = entry

    @property
    def unique_id(self) -> str:
        panel: AlarmPanel = self.coordinator.data
        return panel_unique_id(panel.id)

    @property
    def device_info(self) -> DeviceInfo:
        panel: AlarmPanel = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, panel.id)},
            name=panel.name,
            manufacturer="Alula",
        )

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        if self.coordinator.data is None:
            return None
        return _STATE_MAP.get(self.coordinator.data.arm_state)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Exit/entry delay seconds left (as of last_updated) and the target mode."""
        panel: AlarmPanel | None = self.coordinator.data
        if panel is None:
            return {}
        now = time.time()
        return {
            "exit_delay_remaining": panel.exit_delay_remaining(now),
            "entry_delay_remaining": panel.entry_delay_remaining(now),
            "arming_to": panel.arming_to,
        }

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        if not code:
            _LOGGER.error("Disarm requires a PIN code")
            raise ServiceValidationError("Disarm requires the alarm code")
        _LOGGER.info("Alula disarm requested (code not logged)")
        try:
            await self._client.disarm(pin=code)
        except (AlulaApiError, AlulaAuthError) as err:
            _LOGGER.error("Alula disarm failed: %s", err)
            raise HomeAssistantError(f"Alula disarm failed: {err}") from err
        self.coordinator.async_burst_refresh()

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Arm away with native forceArm. The code is not used or stored."""
        del code
        await self._client.force_arm("away")
        self.coordinator.async_burst_refresh()

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Arm home with native forceArm. The code is not used or stored."""
        del code
        await self._client.force_arm("home")
        self.coordinator.async_burst_refresh()

    def _night_arm_variant(self) -> str:
        """Payload shape for Arm Night. Default is forcearm until proven."""
        options = getattr(self._entry, "options", None) or {}
        chosen = options.get(CONF_NIGHT_ARM_VARIANT, DEFAULT_NIGHT_ARM_VARIANT)
        if chosen in FORCE_ARM_VARIANTS:
            return str(chosen)
        return DEFAULT_NIGHT_ARM_VARIANT

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Concord 4 Arm Night: Stay, silent exit, no entry delay.

        Concord 4 has no night level, so level 3 is not sent. The code
        argument is ignored and is not stored. ``armSilent`` and
        ``noEntryDelay`` are unverified on a Concord 4. The payload shape
        comes from the integration option ``night_arm_variant``.
        """
        del code
        variant = self._night_arm_variant()
        kwargs = {}
        if variant in PROTEST_ANSWER_VARIANTS:
            kwargs["exclude_indexes"] = life_safety_zone_indexes(self.hass)
        await self._client.arm_with_variant(
            "home",
            variant,
            silent=True,
            no_entry_delay=True,
            **kwargs,
        )
        self.coordinator.async_burst_refresh()

    async def async_alarm_arm_custom_bypass(self, code: str | None = None) -> None:
        """Arm home with native forceArm. The code is not used or stored."""
        del code
        await self._client.force_arm("home")
        self.coordinator.async_burst_refresh()
