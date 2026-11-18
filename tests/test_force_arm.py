"""Wire-level tests for force arm and zone bypass. No panel and no PIN."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest

from pyalula.client import AlulaApiError, AlulaClient
from pyalula.const import ARM_LEVEL_AWAY, ARM_LEVEL_NIGHT, ARM_LEVEL_STAY
from pyalula.models import Zone, ZoneType


class _FakeWs:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []

    async def send_json(self, msg: dict) -> None:
        self.sent.append(msg)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> AlulaClient:
    monkeypatch.setattr("pyalula.client.BYPASS_WRITE_INTERVAL", 0)
    monkeypatch.setattr("pyalula.client.BYPASS_BEFORE_ARM_DELAY", 0)
    panel = AlulaClient()
    panel._panel_id = "panel-1"
    panel._user_id = "user-1"
    panel._ws = _FakeWs()
    panel._ensure_ws = AsyncMock()
    panel.fetch_zone_statuses = AsyncMock(return_value=[])
    return panel


def _messages(panel: AlulaClient) -> list[dict]:
    return panel._ws.sent  # type: ignore[attr-defined]


def _walk_keys(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(str(key))
            found.extend(_walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_keys(item))
    return found


def test_force_arm_levels(client: AlulaClient) -> None:
    # checked in the async test below via parametrization
    assert ARM_LEVEL_STAY == 2
    assert ARM_LEVEL_NIGHT == 3
    assert ARM_LEVEL_AWAY == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "level"),
    [("home", 2), ("stay", 2), ("night", 3), ("away", 4)],
)
async def test_native_force_arm_payload(client: AlulaClient, mode: str, level: int) -> None:
    await client.force_arm(mode)
    msg = _messages(client)[0]
    assert msg["channel"] == "device.helix"
    inner = msg["send"]
    assert inner["cmdrsp"] == "writeMfd"
    assert inner["deviceId"] == "panel-1"
    assert inner["requestId"] == "user-1"
    assert inner["bypassCache"] is True
    assert inner["payload"] == [{
        "name": "forceArm",
        "value": {"armingLevelValue": level, "userNumber": 0},
    }]
    assert "pin" not in _walk_keys(msg)


@pytest.mark.asyncio
async def test_native_force_arm_rejects_unknown_mode(client: AlulaClient) -> None:
    with pytest.raises(ValueError):
        await client.force_arm("vacation")
    assert _messages(client) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("bypass", "name"), [(True, "zoneBypass"), (False, "zoneUnbypass")])
async def test_zone_bypass_payload(client: AlulaClient, bypass: bool, name: str) -> None:
    await client.set_zone_bypass(5, bypass)
    inner = _messages(client)[0]["send"]
    assert inner["payload"] == [{
        "name": name,
        "indexFirst": 5,
        "indexLast": 5,
        "items": [{"index": 5, "value": {"userNumber": 0}}],
    }]
    assert "pin" not in _walk_keys(inner)


@pytest.mark.asyncio
@pytest.mark.parametrize("zone_index", ["5", 1.5, True])
async def test_zone_bypass_rejects_bad_type(client: AlulaClient, zone_index: object) -> None:
    with pytest.raises(TypeError):
        await client.set_zone_bypass(zone_index)  # type: ignore[arg-type]
    assert _messages(client) == []


@pytest.mark.asyncio
async def test_zone_bypass_rejects_negative_index(client: AlulaClient) -> None:
    with pytest.raises(ValueError):
        await client.set_zone_bypass(-1)
    assert _messages(client) == []


@pytest.mark.asyncio
async def test_bypass_then_arm_uses_existing_arm_without_pin(client: AlulaClient) -> None:
    client._zones = {
        "2": Zone(id="2", name="Front door", zone_type=ZoneType.DOOR, is_open=False),
        "5": Zone(
            id="5",
            name="Kitchen window",
            zone_type=ZoneType.WINDOW,
            is_open=True,
            is_bypassed=True,
        ),
        "1": Zone(id="1", name="Hall motion", zone_type=ZoneType.MOTION, is_open=True),
        "9": Zone(id="9", name="Smoke", zone_type=ZoneType.SMOKE, is_open=True),
        "8": Zone(id="8", name="CO", zone_type=ZoneType.CO, is_open=True),
        "x": Zone(id="x", name="Bad", zone_type=ZoneType.OTHER, is_open=True),
    }
    indexes = await client.arm_bypassing_open("home")
    assert indexes == [1, 5]
    client.fetch_zone_statuses.assert_awaited_once_with(device_id="panel-1")

    sent = _messages(client)
    assert [item["send"]["payload"][0]["name"] for item in sent] == [
        "zoneBypass",
        "zoneBypass",
        "armingLevel",
    ]
    assert sent[0]["send"]["payload"][0]["indexFirst"] == 1
    assert sent[1]["send"]["payload"][0]["indexFirst"] == 5

    arm = sent[2]["send"]
    assert arm["cmdrsp"] == "writeMfd"
    assert "bypassCache" not in arm
    assert arm["payload"] == [{
        "name": "armingLevel",
        "value": {
            "armingLevelValue": ARM_LEVEL_STAY,
            "userNumber": 0,
            "armSilent": False,
            "noEntryDelay": False,
        },
    }]
    assert "pin" not in _walk_keys(sent)
    assert "changeArmingLevelUsingCode" not in json.dumps(sent)


@pytest.mark.asyncio
async def test_bypass_then_arm_with_no_open_zones_only_arms(client: AlulaClient) -> None:
    client._zones = {
        "0": Zone(id="0", name="Closed", zone_type=ZoneType.DOOR, is_open=False),
    }
    assert await client.arm_bypassing_open("away") == []
    sent = _messages(client)
    assert len(sent) == 1
    assert sent[0]["send"]["payload"][0]["name"] == "armingLevel"
    assert sent[0]["send"]["payload"][0]["value"]["armingLevelValue"] == ARM_LEVEL_AWAY


@pytest.mark.asyncio
async def test_invalid_mode_does_not_bypass(client: AlulaClient) -> None:
    client._zones = {
        "1": Zone(id="1", name="Window", zone_type=ZoneType.DOOR, is_open=True),
    }
    with pytest.raises(ValueError):
        await client.arm_bypassing_open("vacation")
    client.fetch_zone_statuses.assert_not_awaited()
    assert _messages(client) == []


@pytest.mark.asyncio
async def test_zone_refresh_failure_uses_cached_open_zones(client: AlulaClient) -> None:
    client.fetch_zone_statuses = AsyncMock(side_effect=AlulaApiError("timeout"))
    client._zones = {
        "3": Zone(id="3", name="Window", zone_type=ZoneType.DOOR, is_open=True),
    }
    assert await client.arm_bypassing_open("night") == [3]
    assert _messages(client)[0]["send"]["payload"][0]["name"] == "zoneBypass"
    assert _messages(client)[1]["send"]["payload"][0]["value"]["armingLevelValue"] == ARM_LEVEL_NIGHT


@pytest.mark.asyncio
async def test_disarm_log_redacts_pin(client: AlulaClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="pyalula.client")
    await client.disarm("1234")
    sent = _messages(client)[0]
    assert sent["send"]["payload"]["pin"] == ["1", "2", "3", "4"]
    assert "['1', '2', '3', '4']" not in caplog.text
    assert "'pin': '***'" in caplog.text


@pytest.mark.asyncio
async def test_force_arm_requires_panel_id() -> None:
    panel = AlulaClient()
    with pytest.raises(AlulaApiError):
        await panel.force_arm("home")
