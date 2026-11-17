"""Alula force-arm and zone-bypass services.

Neither service asks for or stores a PIN. Disarm is unchanged and still
takes the code on the alarm panel entity.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    ATTR_BYPASS,
    ATTR_METHOD,
    ATTR_MODE,
    ATTR_ZONE,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DEFAULT_FORCE_ARM_METHOD,
    DOMAIN,
    FORCE_ARM_METHODS,
    FORCE_ARM_MODES,
    SERVICE_BYPASS_ZONE,
    SERVICE_FORCE_ARM,
)
from .coordinator import AlulaCoordinator
from .identifiers import zone_index_from_unique_id
from .pyalula.client import AlulaApiError, AlulaAuthError, AlulaClient

_LOGGER = logging.getLogger(__name__)

FORCE_ARM_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): cv.entity_ids,
        vol.Required(ATTR_MODE): vol.In(FORCE_ARM_MODES),
        vol.Optional(ATTR_METHOD, default=DEFAULT_FORCE_ARM_METHOD): vol.In(
            FORCE_ARM_METHODS
        ),
    }
)


def _bypass_zone_has_target(data: dict) -> dict:
    if not data.get("entity_id") and data.get(ATTR_ZONE) is None:
        raise vol.Invalid("Provide a zone entity_id or a zone index")
    return data


BYPASS_ZONE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional("entity_id"): cv.entity_ids,
            vol.Optional(ATTR_ZONE): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
            vol.Optional(ATTR_BYPASS, default=True): cv.boolean,
        }
    ),
    _bypass_zone_has_target,
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register force arm and zone bypass once."""
    if hass.services.has_service(DOMAIN, SERVICE_FORCE_ARM):
        return

    async def _force_arm(call: ServiceCall) -> None:
        await async_handle_force_arm(hass, call)

    async def _bypass_zone(call: ServiceCall) -> None:
        await async_handle_bypass_zone(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_FORCE_ARM, _force_arm, schema=FORCE_ARM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_BYPASS_ZONE, _bypass_zone, schema=BYPASS_ZONE_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the services. Call only after the last config entry unloads."""
    for service in (SERVICE_FORCE_ARM, SERVICE_BYPASS_ZONE):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


async def async_handle_force_arm(hass: HomeAssistant, call: ServiceCall) -> None:
    """Arm one or more panels, bypassing open zones unless method is native."""
    mode = str(call.data[ATTR_MODE]).lower()
    method = str(call.data.get(ATTR_METHOD, DEFAULT_FORCE_ARM_METHOD)).lower()
    panels = _panels_for_force_arm(hass, _entity_ids(call))
    _LOGGER.info("Alula force arm mode=%s method=%s", mode, method)
    refreshed: list[AlulaCoordinator] = []
    try:
        for client, coordinator in panels:
            if coordinator not in refreshed:
                refreshed.append(coordinator)
            await _invoke(_dispatch_force_arm(client, mode, method))
    finally:
        for coordinator in refreshed:
            await coordinator.async_request_refresh()


async def async_handle_bypass_zone(hass: HomeAssistant, call: ServiceCall) -> None:
    """Bypass or restore a zone by entity or by 0-based index."""
    bypass = bool(call.data.get(ATTR_BYPASS, True))
    zone_arg = call.data.get(ATTR_ZONE)
    entity_ids = _entity_ids(call)
    jobs = _zones_for_bypass(hass, entity_ids, zone_arg)
    _LOGGER.info(
        "Alula %s zone(s) %s",
        "bypass" if bypass else "unbypass",
        [index for _, _, index in jobs],
    )
    refreshed: list[AlulaCoordinator] = []
    try:
        for client, coordinator, index in jobs:
            if coordinator not in refreshed:
                refreshed.append(coordinator)
            await _invoke(client.set_zone_bypass(index, bypass))
    finally:
        for coordinator in refreshed:
            await coordinator.async_request_refresh()


def _entity_ids(call: ServiceCall) -> list[str]:
    raw = call.data.get("entity_id")
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(entity_id) for entity_id in raw]


def _panels_for_force_arm(
    hass: HomeAssistant, entity_ids: list[str]
) -> list[tuple[AlulaClient, AlulaCoordinator]]:
    if not entity_ids:
        sole = _sole_bundle(hass)
        if sole is not None:
            return [sole]
        if not hass.data.get(DOMAIN):
            raise HomeAssistantError("Alula is not set up")
        raise ServiceValidationError(
            "entity_id is required when more than one Alula panel is configured"
        )

    panels: list[tuple[AlulaClient, AlulaCoordinator]] = []
    seen: set[str] = set()
    registry = er.async_get(hass)
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        if entry is None or entry.platform != DOMAIN or not entry.config_entry_id:
            raise ServiceValidationError(f"{entity_id} is not an Alula entity")
        if not entity_id.startswith("alarm_control_panel."):
            raise ServiceValidationError(
                f"{entity_id} is not an Alula alarm panel. "
                "force_arm targets the alarm panel; use bypass_zone for a zone."
            )
        if entry.config_entry_id in seen:
            continue
        seen.add(entry.config_entry_id)
        panels.append(_bundle(hass, entry.config_entry_id))
    return panels


def _zones_for_bypass(
    hass: HomeAssistant,
    entity_ids: list[str],
    zone_arg: int | None,
) -> list[tuple[AlulaClient, AlulaCoordinator, int]]:
    if not entity_ids:
        if zone_arg is None:
            raise ServiceValidationError("Provide a zone entity_id or a zone index")
        sole = _sole_bundle(hass)
        if sole is None:
            if not hass.data.get(DOMAIN):
                raise HomeAssistantError("Alula is not set up")
            raise ServiceValidationError(
                "entity_id is required when more than one Alula panel is configured"
            )
        client, coordinator = sole
        return [(client, coordinator, int(zone_arg))]

    jobs: list[tuple[AlulaClient, AlulaCoordinator, int]] = []
    registry = er.async_get(hass)
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        if entry is None or entry.platform != DOMAIN or not entry.config_entry_id:
            raise ServiceValidationError(f"{entity_id} is not an Alula entity")
        client, coordinator = _bundle(hass, entry.config_entry_id)
        domain = entity_id.split(".", 1)[0]
        if domain == "binary_sensor":
            index = zone_index_from_unique_id(entry.unique_id)
            if index is None:
                raise ServiceValidationError(f"{entity_id} is not an Alula zone sensor")
            if zone_arg is not None and int(zone_arg) != index:
                raise ServiceValidationError(
                    f"{entity_id} is zone {index}, which does not match zone {zone_arg}"
                )
            jobs.append((client, coordinator, index))
            continue
        if domain == "alarm_control_panel":
            if zone_arg is None:
                raise ServiceValidationError(
                    "Pass the zone sensor in entity_id, or pass zone as the "
                    "0-based Alula zone index"
                )
            jobs.append((client, coordinator, int(zone_arg)))
            continue
        raise ServiceValidationError(
            f"{entity_id} is not an Alula zone sensor or alarm panel"
        )
    return jobs


def _bundle(
    hass: HomeAssistant, entry_id: str
) -> tuple[AlulaClient, AlulaCoordinator]:
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    if data is None:
        raise ServiceValidationError("That Alula panel is not loaded")
    return data[DATA_CLIENT], data[DATA_COORDINATOR]


def _sole_bundle(
    hass: HomeAssistant,
) -> tuple[AlulaClient, AlulaCoordinator] | None:
    bucket = hass.data.get(DOMAIN, {})
    if len(bucket) != 1:
        return None
    return _bundle(hass, next(iter(bucket)))


async def _dispatch_force_arm(client: AlulaClient, mode: str, method: str) -> None:
    if method == "native":
        await client.force_arm(mode)
        return
    if method == "bypass":
        await client.arm_bypassing_open(mode)
        return
    raise ServiceValidationError(
        f"Invalid force arm method {method!r}. Use: bypass, native"
    )


async def _invoke(coro) -> None:
    try:
        await coro
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    except (AlulaApiError, AlulaAuthError) as err:
        raise HomeAssistantError(f"Alula command failed: {err}") from err
