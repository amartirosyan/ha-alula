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
    monkeypatch.setattr("pyalula.client.FORCEARM_THEN_LEVEL_DELAY", 0)
    monkeypatch.setattr("pyalula.client.PROTEST_REPLY_TIMEOUT", 0.01)
    monkeypatch.setattr("pyalula.client.PROTEST_SETTLE_DELAY", 0)
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


def test_followup_delay_is_one_second() -> None:
    from pyalula.client import FORCEARM_THEN_LEVEL_DELAY

    assert FORCEARM_THEN_LEVEL_DELAY == 1.0


@pytest.mark.asyncio
async def test_force_arm_flags_only_when_requested(client: AlulaClient) -> None:
    await client.force_arm("home", silent=True, no_entry_delay=True)
    value = _messages(client)[0]["send"]["payload"][0]["value"]
    assert value == {
        "armingLevelValue": ARM_LEVEL_STAY,
        "userNumber": 0,
        "armSilent": True,
        "noEntryDelay": True,
    }
    assert "pin" not in _walk_keys(_messages(client))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "silent", "no_entry_delay"),
    [
        ("forcearm", True, True),
        ("forcearm", False, False),
    ],
)
async def test_variant_forcearm_always_includes_flags(
    client: AlulaClient, variant: str, silent: bool, no_entry_delay: bool
) -> None:
    await client.arm_with_variant(
        "home", variant, silent=silent, no_entry_delay=no_entry_delay
    )
    value = _messages(client)[0]["send"]["payload"][0]["value"]
    assert value == {
        "armingLevelValue": ARM_LEVEL_STAY,
        "userNumber": 0,
        "armSilent": silent,
        "noEntryDelay": no_entry_delay,
    }
    assert "pin" not in _walk_keys(_messages(client))


@pytest.mark.asyncio
async def test_variant_forcearm_night_stays_level_3(client: AlulaClient) -> None:
    """The service mode night is still level 3. Arm Night on the entity remaps."""
    await client.arm_with_variant("night", "forcearm", silent=True, no_entry_delay=True)
    value = _messages(client)[0]["send"]["payload"][0]["value"]
    assert value["armingLevelValue"] == ARM_LEVEL_NIGHT


@pytest.mark.asyncio
async def test_variant_arminglevel_payload(client: AlulaClient) -> None:
    await client.arm_with_variant("away", "arminglevel", silent=True, no_entry_delay=False)
    arm = _messages(client)[0]["send"]
    assert arm["cmdrsp"] == "writeMfd"
    assert "bypassCache" not in arm
    assert arm["payload"] == [{
        "name": "armingLevel",
        "value": {
            "armingLevelValue": ARM_LEVEL_AWAY,
            "userNumber": 0,
            "armSilent": True,
            "noEntryDelay": False,
        },
    }]
    assert "pin" not in _walk_keys(arm)


@pytest.mark.asyncio
async def test_variant_partition_has_no_pin(client: AlulaClient) -> None:
    await client.arm_with_variant(
        "home", "partition", silent=True, no_entry_delay=True
    )
    inner = _messages(client)[0]["send"]
    assert inner["cmdrsp"] == "partitionArmingLevelChange"
    assert inner["deviceId"] == "panel-1"
    assert "bypassCache" not in inner
    assert inner["requestId"] != "user-1"
    assert inner["payload"] == {
        "armingLevel": "stay",
        "partitions": [True, False, False, False, False, False, False, False],
        "armSilent": True,
        "noEntryDelay": True,
        "authType": "user",
        "userNumber": 0,
        "forceArm": False,
    }
    assert "pin" not in _walk_keys(inner)


@pytest.mark.asyncio
async def test_variant_forcearm_then_arminglevel(
    client: AlulaClient,
) -> None:
    await client.arm_with_variant(
        "home",
        "forcearm_then_arminglevel",
        silent=True,
        no_entry_delay=True,
    )
    sent = _messages(client)
    assert [item["send"]["payload"][0]["name"] for item in sent] == [
        "forceArm",
        "armingLevel",
    ]
    assert sent[0]["send"]["payload"][0]["value"] == {
        "armingLevelValue": ARM_LEVEL_STAY,
        "userNumber": 0,
    }
    assert "armSilent" not in sent[0]["send"]["payload"][0]["value"]
    assert sent[0]["send"]["bypassCache"] is True
    assert sent[1]["send"]["payload"] == [{
        "name": "armingLevel",
        "value": {
            "armingLevelValue": ARM_LEVEL_STAY,
            "userNumber": 0,
            "armSilent": True,
            "noEntryDelay": True,
        },
    }]
    assert "bypassCache" not in sent[1]["send"]
    assert "pin" not in _walk_keys(sent)


@pytest.mark.asyncio
async def test_unknown_variant_sends_nothing(client: AlulaClient) -> None:
    with pytest.raises(ValueError):
        await client.arm_with_variant("home", "level3")
    assert _messages(client) == []


@pytest.mark.asyncio
async def test_arm_commands_and_ack_nak_log_at_info(
    client: AlulaClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="pyalula.client")
    await client.force_arm("home", silent=True, no_entry_delay=True)
    assert "Alula WS command" in caplog.text
    assert "forceArm" in caplog.text
    assert "armSilent" in caplog.text
    assert "'pin'" not in caplog.text

    request_id = _messages(client)[0]["send"]["requestId"]
    await client._handle_ws_message({
        "event": {
            "data": {
                "cmdrsp": "ack",
                "requestId": request_id,
                "payload": {},
            }
        }
    })
    assert "Alula panel ACK" in caplog.text

    await client.arm_with_variant("home", "partition", silent=True, no_entry_delay=True)
    partition_id = _messages(client)[-1]["send"]["requestId"]
    await client._handle_ws_message({
        "event": {
            "data": {
                "cmdrsp": "nak",
                "requestId": partition_id,
                "payload": {
                    "nakReasons": [{"reason": "unsupported command"}],
                    "pin": ["9", "9", "9", "9"],
                },
            }
        }
    })
    assert "Alula panel NAK" in caplog.text
    assert "unsupported command" in caplog.text
    assert "9999" not in caplog.text
    assert "'pin': '***'" in caplog.text
    assert "['9', '9', '9', '9']" not in caplog.text


@pytest.mark.asyncio
async def test_unrelated_ack_is_not_an_arm_reply(
    client: AlulaClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="pyalula.client")
    await client.force_arm("home")
    await client._handle_ws_message({
        "event": {"data": {"cmdrsp": "ack", "requestId": "someone-else"}}
    })
    assert "Alula panel ACK" not in caplog.text
    assert client._arm_inflight


@pytest.mark.asyncio
async def test_ack_without_request_id_still_logs_while_arming(
    client: AlulaClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="pyalula.client")
    await client.force_arm("home")
    await client._handle_ws_message({"event": {"data": {"cmdrsp": "ACK"}}})
    assert "Alula panel ACK" in caplog.text
    assert client._arm_inflight == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "second"),
    [
        ("arminglevel_then_forcearm", ["forceArm"]),
        ("arminglevel_then_rearm", ["armingLevel"]),
        ("arminglevel_then_zonebypass", ["zoneBypass", "zoneBypass"]),
    ],
)
async def test_protest_answer_variants(
    client: AlulaClient, monkeypatch: pytest.MonkeyPatch, variant: str, second: list[str]
) -> None:
    monkeypatch.setattr("pyalula.client.PROTEST_ANSWER_DELAY", 0)
    client._zones = {
        8: Zone(id="8", name="GUEST BEDROOM WINDOW", zone_type=ZoneType.WINDOW, is_open=True),
        12: Zone(id="12", name="BEDROOM 3 WINDOW", zone_type=ZoneType.WINDOW, is_open=True),
        5: Zone(id="5", name="SMOKE", zone_type=ZoneType.SMOKE, is_open=True),
        1: Zone(id="1", name="FRONT DOOR", zone_type=ZoneType.WINDOW, is_open=False),
    }
    await client.arm_with_variant("home", variant, silent=True, no_entry_delay=True)
    msgs = _messages(client)
    names = [m["send"]["payload"][0]["name"] for m in msgs]
    assert names == ["armingLevel"] + second
    first = msgs[0]["send"]["payload"][0]["value"]
    assert first == {
        "armingLevelValue": 2,
        "userNumber": 0,
        "armSilent": True,
        "noEntryDelay": True,
    }
    if variant == "arminglevel_then_forcearm":
        assert msgs[1]["send"]["payload"][0]["value"] == {"armingLevelValue": 2, "userNumber": 0}
    if variant == "arminglevel_then_rearm":
        assert msgs[1]["send"]["payload"][0]["value"] == first
    if variant == "arminglevel_then_zonebypass":
        assert [m["send"]["payload"][0]["indexFirst"] for m in msgs[1:]] == [8, 12]
    for m in msgs:
        assert "pin" not in _walk_keys(m)


@pytest.mark.asyncio
async def test_protest_answer_skipped_when_no_open_zones(
    client: AlulaClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pyalula.client.PROTEST_ANSWER_DELAY", 0)
    client._zones = {}
    await client.arm_with_variant("home", "arminglevel_then_forcearm", silent=True, no_entry_delay=True)
    assert [m["send"]["payload"][0]["name"] for m in _messages(client)] == ["armingLevel"]


def _windows_open(panel: AlulaClient) -> None:
    panel._zones = {
        8: Zone(id="8", name="GUEST BEDROOM WINDOW", zone_type=ZoneType.WINDOW, is_open=True),
        12: Zone(id="12", name="BEDROOM 3WINDOW", zone_type=ZoneType.WINDOW, is_open=True),
    }


def _nak_frame(request_id: str) -> dict:
    return {
        "channel": "device.helix",
        "event": {
            "data": {
                "cmdrsp": "nak",
                "payload": {"nakReasons": [{"reason": "Protest Open Zones"}]},
                "requestId": request_id,
            }
        },
    }


@pytest.mark.asyncio
async def test_zonebypass_answers_nak_with_open_zones(
    client: AlulaClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Live-proven flow: armingLevel, protest NAK, then zoneBypass per open zone."""
    monkeypatch.setattr("pyalula.client.PROTEST_ANSWER_DELAY", 0)
    monkeypatch.setattr("pyalula.client.PROTEST_REPLY_TIMEOUT", 5)
    _windows_open(client)
    ws = client._ws
    real_send = ws.send_json

    async def send_and_nak(msg: dict) -> None:
        await real_send(msg)
        if msg["send"]["payload"][0]["name"] == "armingLevel":
            client._log_arm_reply(_nak_frame(msg["send"]["requestId"]))

    ws.send_json = send_and_nak
    caplog.set_level(logging.INFO, logger="pyalula.client")
    loop = __import__("asyncio").get_running_loop()
    t0 = loop.time()
    answered = await client.arm_with_variant(
        "home", "arminglevel_then_zonebypass", silent=True, no_entry_delay=True
    )
    assert loop.time() - t0 < 2  # did not sit out the 5 s reply timeout
    msgs = _messages(client)
    assert [m["send"]["payload"][0]["name"] for m in msgs] == [
        "armingLevel", "zoneBypass", "zoneBypass"
    ]
    assert [m["send"]["payload"][0]["indexFirst"] for m in msgs[1:]] == [8, 12]
    assert "Protest Open Zones" in caplog.text
    assert "NAK" in caplog.text
    client.fetch_zone_statuses.assert_awaited()


@pytest.mark.asyncio
async def test_zonebypass_uses_zone_state_after_the_wait(
    client: AlulaClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window that opens before the answer is bypassed; a closed one is not."""
    monkeypatch.setattr("pyalula.client.PROTEST_ANSWER_DELAY", 0)
    _windows_open(client)
    real_arm = client.arm

    async def arm_then_change(*args, **kwargs):
        await real_arm(*args, **kwargs)
        client._zones[8].is_open = False
        client._zones[3] = Zone(id="3", name="KITCHEN DOOR", zone_type=ZoneType.DOOR, is_open=True)

    client.arm = arm_then_change
    await client.arm_with_variant("home", "arminglevel_then_zonebypass", silent=True, no_entry_delay=True)
    msgs = _messages(client)
    assert [m["send"]["payload"][0]["indexFirst"] for m in msgs[1:]] == [3, 12]


@pytest.mark.asyncio
async def test_zonebypass_never_bypasses_life_safety(
    client: AlulaClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pyalula.client.PROTEST_ANSWER_DELAY", 0)
    _windows_open(client)
    client._zones.update({
        1: Zone(id="1", name="SMOKE", zone_type=ZoneType.SMOKE, is_open=True),
        2: Zone(id="2", name="CO", zone_type=ZoneType.CO, is_open=True),
        4: Zone(id="4", name="BASEMENT WATER", zone_type=ZoneType.OTHER, is_open=True,
                raw={"deviceType": "WATER"}),
        6: Zone(id="6", name="FURNACE HEAT", zone_type=ZoneType.OTHER, is_open=True,
                raw={"deviceType": "HEAT_DET"}),
        7: Zone(id="7", name="GAS", zone_type=ZoneType.OTHER, is_open=True),
    })
    # zone 7 is flagged by Home Assistant's device class (gas)
    await client.arm_with_variant(
        "home", "arminglevel_then_zonebypass", silent=True, no_entry_delay=True,
        exclude_indexes={7},
    )
    msgs = _messages(client)
    assert [m["send"]["payload"][0]["indexFirst"] for m in msgs[1:]] == [8, 12]


@pytest.mark.asyncio
async def test_zonebypass_with_nothing_open_only_arms(
    client: AlulaClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pyalula.client.PROTEST_ANSWER_DELAY", 0)
    client._zones = {
        8: Zone(id="8", name="GUEST BEDROOM WINDOW", zone_type=ZoneType.WINDOW, is_open=False),
    }
    await client.arm_with_variant("home", "arminglevel_then_zonebypass", silent=True, no_entry_delay=True)
    assert [m["send"]["payload"][0]["name"] for m in _messages(client)] == ["armingLevel"]


@pytest.mark.asyncio
async def test_disarm_is_traced_and_pin_redacted(
    client: AlulaClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="pyalula.client")
    await client.disarm("9876")
    sent = _messages(client)[0]
    assert sent["send"]["cmdrsp"] == "changeArmingLevelUsingCode"
    assert sent["send"]["payload"]["armingLevelValue"] == 1
    assert "Alula WS command" in caplog.text
    assert "9876" not in caplog.text
    assert "'9', '8'" not in caplog.text
