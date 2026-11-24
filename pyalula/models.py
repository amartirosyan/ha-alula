"""Data models for the Alula API."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

_LOGGER = logging.getLogger(__name__)


class ArmState(str, Enum):
    DISARMED = "disarm"
    ARMED_HOME = "stay"
    ARMED_AWAY = "away"
    ARMED_NIGHT = "night"
    TRIGGERED = "triggered"
    # Derived from panelStatus, never sent by the REST armingLevel field.
    ARMING = "arming"    # exit delay running
    PENDING = "pending"  # entry delay running

    @classmethod
    def from_api(cls, value: str) -> ArmState:
        """Parse armingLevel string from the REST API."""
        try:
            state = cls(value.lower())
        except ValueError:
            return cls.DISARMED
        if state in (cls.ARMING, cls.PENDING):
            return cls.DISARMED
        return state


class ZoneType(str, Enum):
    DOOR = "door"
    WINDOW = "window"
    MOTION = "motion"
    SMOKE = "smoke"
    CO = "co"
    GLASS_BREAK = "glass_break"
    OTHER = "other"


@dataclass
class Zone:
    id: str            # e.g. "1", "2" — zone index
    name: str
    zone_type: ZoneType
    is_open: bool      # True = faulted/open/triggered
    is_bypassed: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_ws(cls, data: dict) -> Zone:
        """Parse a zone entry from a WebSocket zoneStatus response.

        The response shape is inferred from the smali; update if it differs:
        {
          "index": 1,
          "name": "Front Door",
          "type": "door",           # may be absent
          "open": true,
          "bypassed": false
        }
        """
        index = data.get("index", data.get("zoneIndex", data.get("id", "?")))
        return cls(
            id=str(index),
            name=data.get("name", data.get("zoneName", f"Zone {index}")),
            zone_type=_parse_zone_type(data.get("type", data.get("zoneType", ""))),
            is_open=bool(data.get("open", data.get("faulted", data.get("status", 0)))),
            is_bypassed=bool(data.get("bypassed", data.get("bypass", False))),
            raw=data,
        )


def _parse_zone_type(raw: str) -> ZoneType:
    mapping = {
        "door": ZoneType.DOOR,
        "window": ZoneType.WINDOW,
        "motion": ZoneType.MOTION,
        "smoke": ZoneType.SMOKE,
        "co": ZoneType.CO,
        "glass": ZoneType.GLASS_BREAK,
        "glass_break": ZoneType.GLASS_BREAK,
    }
    return mapping.get(str(raw).lower(), ZoneType.OTHER)


# panelStatus.armingLevel values (helix): 1 disarm, 2 stay, 3 night, 4 away.
_ARMING_LEVEL_TO = {2: "stay", 3: "night", 4: "away"}


def _parse_iso(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class PanelStatus:
    """The helix ``panelStatus`` object (pushed on device.helix).

    ``entryExitDelayTimer`` is the delay length when the delay starts, not a
    live countdown, so the time left is worked out from ``started_at``
    (``lastCachedAt`` when the cloud sends it, else the time it arrived).
    The panel does not push when an exit delay ends; poll while one runs.
    """

    in_exit_delay: bool = False
    in_entry_delay: bool = False
    delay_timer: int = 0
    arming_level: int | None = None
    arming_silent: bool = False
    arming_no_delay: bool = False
    status_version: int | None = None
    received_at: float = 0.0  # wall clock (time.time()) when first seen
    started_at: float = 0.0   # wall clock the current snapshot began

    @classmethod
    def from_payload(cls, item: dict, received_at: float) -> PanelStatus | None:
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(value, dict):
            return None

        def _int(key: str) -> int | None:
            raw = value.get(key)
            try:
                return int(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None

        cached = _parse_iso(item.get("lastCachedAt"))
        started = received_at
        # Use the cloud's cache time only when it is plausible (clock skew).
        if cached is not None and 0 <= received_at - cached <= 600:
            started = cached
        return cls(
            in_exit_delay=bool(value.get("inExitDelay", False)),
            in_entry_delay=bool(value.get("inEntryDelay", False)),
            delay_timer=max(_int("entryExitDelayTimer") or 0, 0),
            arming_level=_int("armingLevel"),
            arming_silent=bool(value.get("armingSilent", False)),
            arming_no_delay=bool(value.get("armingWithNoDelay", False)),
            status_version=_int("statusVersion"),
            received_at=received_at,
            started_at=started,
        )

    @property
    def in_delay(self) -> bool:
        return self.in_exit_delay or self.in_entry_delay

    def remaining(self, now: float) -> int:
        """Whole seconds left in the running delay (0 when none)."""
        if not self.in_delay or self.delay_timer <= 0:
            return 0
        left = self.delay_timer - (now - self.started_at)
        return max(0, int(math.ceil(left)))

    @property
    def arming_to(self) -> str | None:
        target = _ARMING_LEVEL_TO.get(self.arming_level or 0)
        if target == "stay" and self.arming_silent and self.arming_no_delay:
            # Concord 4 has no night level: Night is silent Stay, no delay.
            return "night"
        return target


@dataclass
class AlarmPanel:
    id: str
    name: str
    arm_state: ArmState
    zones: list[Zone] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)
    # REST armingLevel as parsed; arm_state may be ARMING/PENDING on top.
    rest_state: ArmState | None = None
    status: PanelStatus | None = None
    # Wall clock of the REST read behind rest_state (0 = unknown).
    rest_fetched_at: float = 0.0

    def apply_status(self, status: PanelStatus | None, now: float) -> ArmState:
        """Set arm_state from the REST state plus a running panel delay."""
        base = self.rest_state if self.rest_state is not None else self.arm_state
        self.rest_state = base
        self.status = status
        state = base
        if (
            status is not None
            and status.arming_level == 1
            and not status.in_delay
            and self.rest_fetched_at
            and status.received_at > self.rest_fetched_at
        ):
            # A panelStatus newer than the REST read says disarmed: REST lags.
            state = ArmState.DISARMED
        elif status is not None and status.remaining(now) > 0:
            if status.in_exit_delay and status.arming_to is not None:
                state = ArmState.ARMING
            elif status.in_entry_delay and base not in (
                ArmState.DISARMED, ArmState.TRIGGERED
            ):
                state = ArmState.PENDING
        self.arm_state = state
        return state

    def exit_delay_remaining(self, now: float) -> int:
        if self.arm_state != ArmState.ARMING or self.status is None:
            return 0
        return self.status.remaining(now)

    def entry_delay_remaining(self, now: float) -> int:
        if self.arm_state != ArmState.PENDING or self.status is None:
            return 0
        return self.status.remaining(now)

    @property
    def arming_to(self) -> str | None:
        if self.arm_state != ArmState.ARMING or self.status is None:
            return None
        return self.status.arming_to

    @classmethod
    def from_api(cls, data: dict, zones: list[Zone] | None = None) -> AlarmPanel:
        """Parse panel from REST GET /rest/v1/devices response data item."""
        attrs = data.get("attributes", data)
        raw_arming = attrs.get("armingLevel", "disarm")
        _LOGGER.debug("REST armingLevel raw value: %r", raw_arming)
        arm_state = ArmState.from_api(raw_arming)
        _LOGGER.debug("REST arm_state parsed: %s", arm_state)
        return cls(
            id=str(data.get("id", "")),
            name=attrs.get("friendlyName", "Alula Panel"),
            arm_state=arm_state,
            zones=zones or [],
            raw=data,
            rest_state=arm_state,
        )
