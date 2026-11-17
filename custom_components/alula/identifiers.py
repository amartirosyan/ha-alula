"""Stable entity id helpers for the Alula integration.

The strings produced here are the existing unique ids. Do not change the
format: Home Assistant uses them to keep the current entities.
"""

from __future__ import annotations

ZONE_UNIQUE_ID_PREFIX = "alula_zone_"


def panel_unique_id(panel_id: str) -> str:
    """Unique id for the alarm panel entity."""
    return f"alula_{panel_id}_panel"


def zone_unique_id(zone_id: str) -> str:
    """Unique id for a zone binary sensor."""
    return f"{ZONE_UNIQUE_ID_PREFIX}{zone_id}"


def zone_index_from_unique_id(unique_id: str | None) -> int | None:
    """Return the 0-based zone index stored in a zone unique id."""
    if not unique_id or not unique_id.startswith(ZONE_UNIQUE_ID_PREFIX):
        return None
    suffix = unique_id[len(ZONE_UNIQUE_ID_PREFIX):]
    if not suffix.isdigit():
        return None
    return int(suffix)
