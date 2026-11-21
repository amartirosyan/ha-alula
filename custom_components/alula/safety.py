"""Zones Home Assistant must never auto-bypass when answering an arm protest."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .identifiers import zone_index_from_unique_id

# Fire and life-safety device classes. A zone shown with one of these in
# Home Assistant (including a device class the user set) is never bypassed.
LIFE_SAFETY_DEVICE_CLASSES: frozenset[str] = frozenset(
    {"smoke", "carbon_monoxide", "co", "gas", "moisture", "heat", "safety"}
)


def life_safety_zone_indexes(hass: HomeAssistant) -> set[int]:
    """0-based Alula zone indexes whose HA device class is life safety."""
    found: set[int] = set()
    registry = er.async_get(hass)
    for entry in list(registry.entities.values()):
        if entry.platform != DOMAIN or entry.domain != "binary_sensor":
            continue
        index = zone_index_from_unique_id(entry.unique_id)
        if index is None:
            continue
        classes = {
            str(entry.device_class or "").lower(),
            str(entry.original_device_class or "").lower(),
        }
        state = hass.states.get(entry.entity_id)
        if state is not None:
            classes.add(str(state.attributes.get("device_class") or "").lower())
        if classes & LIFE_SAFETY_DEVICE_CLASSES:
            found.add(index)
    return found
