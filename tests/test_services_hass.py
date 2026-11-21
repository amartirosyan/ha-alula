"""Load the integration inside Home Assistant and exercise the new services."""

from __future__ import annotations

import sys
from inspect import signature
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

import voluptuous as vol
from alula.alarm_control_panel import AlulaAlarmPanel
from alula.config_flow import AlulaOptionsFlow
from alula.const import CONF_NIGHT_ARM_VARIANT, DATA_CLIENT, DATA_COORDINATOR, DOMAIN
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
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

ENTRY_A = "entry-a"
ENTRY_B = "entry-b"


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def force_arm(self, mode: str, **kwargs) -> None:
        if kwargs:
            self.calls.append(("native", mode, kwargs))
        else:
            self.calls.append(("native", mode))

    async def arm_bypassing_open(self, mode: str, **kwargs) -> None:
        if kwargs:
            self.calls.append(("bypass", mode, kwargs))
        else:
            self.calls.append(("bypass", mode))

    async def arm_with_variant(self, mode: str, variant: str, **kwargs) -> None:
        self.calls.append(("variant", mode, variant, kwargs))

    async def set_zone_bypass(self, index: int, bypass: bool = True) -> None:
        self.calls.append(("zone", index, bypass))


class _Coordinator:
    def __init__(self) -> None:
        self.refreshed = 0

    async def async_request_refresh(self) -> None:
        self.refreshed += 1

    def async_burst_refresh(self) -> None:
        self.refreshed += 1


def _call(hass: HomeAssistant, service: str, data: dict) -> ServiceCall:
    """Build a service call on HA 2024.11 and on later cores.

    2025+ takes hass as the first argument. 2024.11 does not.
    """
    if "hass" in signature(ServiceCall.__init__).parameters:
        return ServiceCall(hass, DOMAIN, service, data)
    return ServiceCall(DOMAIN, service, data)


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

    # Registered schema: method defaults to native, and a string entity_id is accepted.
    await hass.services.async_call(
        DOMAIN,
        "force_arm",
        {"entity_id": panel, "mode": "night"},
        blocking=True,
    )
    assert client.calls[-1] == ("native", "night")
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


@pytest.mark.asyncio
async def test_panel_buttons_use_native_force_arm() -> None:
    client = _Client()

    async def _refresh() -> None:
        return None

    coordinator = SimpleNamespace(
        last_update_success=True,
        data=None,
        async_request_refresh=_refresh,
        async_burst_refresh=lambda: None,
    )
    entity = AlulaAlarmPanel(coordinator, client, object())
    await entity.async_alarm_arm_home("1234")
    await entity.async_alarm_arm_away(None)
    await entity.async_alarm_arm_night("1234")
    await entity.async_alarm_arm_custom_bypass("1234")
    assert client.calls == [
        ("native", "home"),
        ("native", "away"),
        ("variant", "home", "forcearm", {"silent": True, "no_entry_delay": True}),
        ("native", "home"),
    ]
    assert "1234" not in repr(client.calls)

    chosen = _Client()
    entry = SimpleNamespace(options={"night_arm_variant": "partition"})
    panel = AlulaAlarmPanel(coordinator, chosen, entry)
    await panel.async_alarm_arm_night(None)
    assert chosen.calls == [
        ("variant", "home", "partition", {"silent": True, "no_entry_delay": True}),
    ]

    fallback = _Client()
    bad = SimpleNamespace(options={"night_arm_variant": "level3"})
    panel = AlulaAlarmPanel(coordinator, fallback, bad)
    await panel.async_alarm_arm_night(None)
    assert fallback.calls[0][2] == "forcearm"


@pytest.mark.asyncio
async def test_force_arm_variant_and_flags(hass: HomeAssistant) -> None:
    client, _coordinator = _install(
        hass, ENTRY_A, "closet_4508_ethel_ave", "kitchen_window", "4"
    )
    panel = "alarm_control_panel.closet_4508_ethel_ave"
    await async_handle_force_arm(
        hass,
        _call(
            hass,
            "force_arm",
            {
                "entity_id": [panel],
                "mode": "home",
                "silent": True,
                "no_entry_delay": True,
                "variant": "arminglevel",
            },
        ),
    )
    assert client.calls[-1] == (
        "variant",
        "home",
        "arminglevel",
        {"silent": True, "no_entry_delay": True},
    )

    await async_handle_force_arm(
        hass,
        _call(
            hass,
            "force_arm",
            {
                "entity_id": [panel],
                "mode": "home",
                "method": "native",
                "silent": True,
                "no_entry_delay": False,
            },
        ),
    )
    assert client.calls[-1] == (
        "native",
        "home",
        {"silent": True, "no_entry_delay": False},
    )

    async_setup_services(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "force_arm",
            {"entity_id": panel, "mode": "home", "variant": "level3"},
            blocking=True,
        )


@pytest.mark.asyncio
async def test_options_flow_stores_night_variant() -> None:
    flow = AlulaOptionsFlow(SimpleNamespace(options={}))
    shown = await flow.async_step_init()
    assert shown["type"] == FlowResultType.FORM
    created = await flow.async_step_init(
        {CONF_NIGHT_ARM_VARIANT: "forcearm_then_arminglevel"}
    )
    assert created["type"] == FlowResultType.CREATE_ENTRY
    assert created["data"] == {
        CONF_NIGHT_ARM_VARIANT: "forcearm_then_arminglevel",
    }
    assert "pin" not in created["data"]
