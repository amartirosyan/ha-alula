"""Load the integration inside Home Assistant and exercise the new services."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

from alula.alarm_control_panel import AlulaAlarmPanel
from alula.const import DATA_CLIENT, DATA_COORDINATOR, DOMAIN
from alula.identifiers import panel_unique_id, zone_unique_id
from alula.services import (
    async_handle_bypass_zone,
    async_handle_force_arm,
    async_setup_services,
    async_unload_services,
)
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntityFeature,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

ENTRY_A = "entry-a"
ENTRY_B = "entry-b"


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def force_arm(self, mode: str) -> None:
        self.calls.append(("native", mode))

    async def arm_bypassing_open(self, mode: str) -> None:
        self.calls.append(("bypass", mode))

    async def set_zone_bypass(self, index: int, bypass: bool = True) -> None:
        self.calls.append(("zone", index, bypass))


class _Coordinator:
    def __init__(self) -> None:
        self.refreshed = 0

    async def async_request_refresh(self) -> None:
        self.refreshed += 1


def _call(hass: HomeAssistant, service: str, data: dict) -> ServiceCall:
    return ServiceCall(hass, DOMAIN, service, data)


@pytest.fixture
async def hass(tmp_path: Path):
    instance = HomeAssistant(str(tmp_path))
    instance.config.components.add(DOMAIN)
    await instance.async_start()
    await er.async_load(instance)
    yield instance
    await instance.async_stop()


def _install(
    hass: HomeAssistant,
    entry_id: str,
    panel_object_id: str,
    zone_object_id: str,
    zone_index: str,
) -> tuple[_Client, _Coordinator]:
    client = _Client()
    coordinator = _Coordinator()
    hass.data.setdefault(DOMAIN, {})[entry_id] = {
        DATA_CLIENT: client,
        DATA_COORDINATOR: coordinator,
    }
    registry = er.async_get(hass)
    config_entry = SimpleNamespace(entry_id=entry_id, pref_disable_new_entities=False)
    registry.async_get_or_create(
        "alarm_control_panel",
        DOMAIN,
        panel_unique_id("panel-1" if entry_id == ENTRY_A else "panel-2"),
        suggested_object_id=panel_object_id,
        config_entry=config_entry,
    )
    registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        zone_unique_id(zone_index),
        suggested_object_id=zone_object_id,
        config_entry=config_entry,
    )
    return client, coordinator


def test_supported_features_keep_arm_modes_and_add_custom_bypass() -> None:
    entity = AlulaAlarmPanel(
        SimpleNamespace(last_update_success=True, data=None), object(), object()
    )
    features = entity.supported_features
    assert int(AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS) == 16
    assert features & AlarmControlPanelEntityFeature.ARM_HOME
    assert features & AlarmControlPanelEntityFeature.ARM_AWAY
    assert features & AlarmControlPanelEntityFeature.ARM_NIGHT
    assert features & AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
    # Away | Home | Night | Custom bypass. Trigger and vacation stay off.
    assert int(features) == 1 | 2 | 4 | 16


@pytest.mark.asyncio
async def test_force_arm_and_bypass_services(hass: HomeAssistant) -> None:
    client, coordinator = _install(
        hass, ENTRY_A, "closet_4508_ethel_ave", "kitchen_window", "4"
    )
    async_setup_services(hass)
    assert hass.services.has_service(DOMAIN, "force_arm")
    assert hass.services.has_service(DOMAIN, "bypass_zone")

    panel = "alarm_control_panel.closet_4508_ethel_ave"
    await async_handle_force_arm(
        hass,
        _call(hass, "force_arm", {"entity_id": [panel], "mode": "home", "method": "bypass"}),
    )
    assert client.calls == [("bypass", "home")]
    assert coordinator.refreshed == 1

    await async_handle_force_arm(
        hass,
        _call(hass, "force_arm", {"entity_id": [panel], "mode": "away", "method": "native"}),
    )
    assert client.calls[-1] == ("native", "away")

    zone = "binary_sensor.kitchen_window"
    await async_handle_bypass_zone(
        hass,
        _call(hass, "bypass_zone", {"entity_id": [zone], "bypass": True}),
    )
    assert client.calls[-1] == ("zone", 4, True)

    await async_handle_bypass_zone(
        hass,
        _call(hass, "bypass_zone", {"entity_id": [panel], "zone": 4, "bypass": False}),
    )
    assert client.calls[-1] == ("zone", 4, False)

    # One panel: a bare zone index is enough.
    await async_handle_bypass_zone(
        hass, _call(hass, "bypass_zone", {"zone": 0, "bypass": True})
    )
    assert client.calls[-1] == ("zone", 0, True)

    with pytest.raises(ServiceValidationError):
        await async_handle_bypass_zone(
            hass,
            _call(hass, "bypass_zone", {"entity_id": [zone], "zone": 1, "bypass": True}),
        )

    # Registered schema: method defaults to bypass, and a string entity_id is accepted.
    await hass.services.async_call(
        DOMAIN,
        "force_arm",
        {"entity_id": panel, "mode": "night"},
        blocking=True,
    )
    assert client.calls[-1] == ("bypass", "night")
    await hass.services.async_call(
        DOMAIN,
        "bypass_zone",
        {"entity_id": zone, "bypass": False},
        blocking=True,
    )
    assert client.calls[-1] == ("zone", 4, False)


@pytest.mark.asyncio
async def test_second_panel_requires_entity_id(hass: HomeAssistant) -> None:
    _install(hass, ENTRY_A, "closet_4508_ethel_ave", "kitchen_window", "4")
    _install(hass, ENTRY_B, "other_panel", "other_window", "2")
    with pytest.raises(ServiceValidationError):
        await async_handle_force_arm(
            hass, _call(hass, "force_arm", {"mode": "home", "method": "bypass"})
        )
    with pytest.raises(ServiceValidationError):
        await async_handle_bypass_zone(
            hass, _call(hass, "bypass_zone", {"zone": 2, "bypass": True})
        )
    async_setup_services(hass)
    assert hass.services.has_service(DOMAIN, "force_arm")
    async_unload_services(hass)
    assert not hass.services.has_service(DOMAIN, "force_arm")
