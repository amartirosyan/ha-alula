"""panelStatus parsing, delay state mapping and log redaction. No panel, no PIN."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pyalula.client import AlulaClient, _redact
from pyalula.models import AlarmPanel, ArmState, PanelStatus

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 21, 23, 11, 16, 691000, tzinfo=timezone.utc).timestamp()


def _status_item(version=5485, exit_=True, entry=False, timer=60, level=2,
                 cached: str | None = None, **extra) -> dict:
    value = {
        "inExitDelay": exit_, "inEntryDelay": entry, "entryExitDelayTimer": timer,
        "armingLevel": level, "armingProtest": False, "openZones": False,
        "bypassedZones": True, "armingSilent": False, "armingWithNoDelay": False,
        "alarmType": 0, "alarmZones": [], "statusVersion": version,
    }
    value.update(extra)
    item = {"name": "panelStatus", "value": value}
    if cached:
        item["lastCachedAt"] = cached
    return item


def _panel(level: str) -> AlarmPanel:
    return AlarmPanel.from_api(
        {"id": "p1", "attributes": {"friendlyName": "Home", "armingLevel": level}}
    )


# ---- parser ---------------------------------------------------------------

def test_parse_exit_delay_push() -> None:
    st = PanelStatus.from_payload(_status_item(), received_at=T0)
    assert st is not None
    assert st.in_exit_delay and not st.in_entry_delay
    assert st.delay_timer == 60 and st.arming_level == 2 and st.status_version == 5485
    assert st.arming_to == "stay"
    assert st.remaining(T0) == 60
    assert st.remaining(T0 + 23.2) == 37
    assert st.remaining(T0 + 60) == 0
    assert st.remaining(T0 + 300) == 0


def test_parse_uses_last_cached_at_for_start() -> None:
    st = PanelStatus.from_payload(
        _status_item(cached="2026-11-21T23:11:16.691Z"), received_at=T0 + 29
    )
    assert st is not None
    assert abs(st.started_at - T0) < 0.01
    assert st.remaining(T0 + 29) == 31


def test_parse_ignores_implausible_cache_time() -> None:
    st = PanelStatus.from_payload(
        _status_item(cached="2020-01-01T00:00:00Z"), received_at=T0
    )
    assert st is not None and st.started_at == T0


def test_parse_rejects_bad_payload() -> None:
    assert PanelStatus.from_payload({"name": "panelStatus"}, received_at=T0) is None
    assert PanelStatus.from_payload({"name": "panelStatus", "value": []}, T0) is None


@pytest.mark.parametrize(
    ("level", "silent", "nodelay", "target"),
    [(2, False, False, "stay"), (3, False, False, "night"), (4, False, False, "away"),
     (2, True, True, "night"), (1, False, False, None)],
)
def test_arming_to(level, silent, nodelay, target) -> None:
    st = PanelStatus.from_payload(
        _status_item(level=level, armingSilent=silent, armingWithNoDelay=nodelay), T0
    )
    assert st is not None and st.arming_to == target


# ---- state mapping --------------------------------------------------------

def test_exit_delay_maps_to_arming_then_rest_state() -> None:
    st = PanelStatus.from_payload(_status_item(), received_at=T0)
    panel = _panel("stay")
    assert panel.apply_status(st, T0 + 5) == ArmState.ARMING
    assert panel.exit_delay_remaining(T0 + 5) == 55
    assert panel.entry_delay_remaining(T0 + 5) == 0
    assert panel.arming_to == "stay"
    # Timer ran out with no new push: fall back to the REST state.
    assert panel.apply_status(st, T0 + 61) == ArmState.ARMED_HOME
    assert panel.exit_delay_remaining(T0 + 61) == 0
    assert panel.arming_to is None


def test_exit_delay_shows_arming_even_if_rest_lags() -> None:
    st = PanelStatus.from_payload(_status_item(level=4), received_at=T0)
    panel = _panel("disarm")
    assert panel.apply_status(st, T0 + 1) == ArmState.ARMING
    assert panel.arming_to == "away"


def test_disarm_push_clears_arming() -> None:
    st = PanelStatus.from_payload(_status_item(exit_=False, level=1, version=5490), T0)
    panel = _panel("disarm")
    assert panel.apply_status(st, T0 + 1) == ArmState.DISARMED


def test_entry_delay_maps_to_pending() -> None:
    st = PanelStatus.from_payload(_status_item(exit_=False, entry=True, timer=30), T0)
    panel = _panel("away")
    assert panel.apply_status(st, T0 + 10) == ArmState.PENDING
    assert panel.entry_delay_remaining(T0 + 10) == 20
    assert panel.exit_delay_remaining(T0 + 10) == 0
    # Entry flag while REST says disarmed (just disarmed) is not pending.
    assert _panel("disarm").apply_status(st, T0 + 10) == ArmState.DISARMED


def test_no_status_keeps_rest_state() -> None:
    panel = _panel("night")
    assert panel.apply_status(None, T0) == ArmState.ARMED_NIGHT
    assert ArmState.from_api("arming") == ArmState.DISARMED


def test_ha_state_map_has_arming_and_pending() -> None:
    src = (ROOT / "custom_components" / "alula" / "alarm_control_panel.py").read_text()
    tree = ast.parse(src)
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_STATE_MAP":
            for k, v in zip(node.value.keys, node.value.values):  # type: ignore[union-attr]
                mapping[ast.unparse(k)] = ast.unparse(v)
    assert mapping["ArmState.ARMING"] == "AlarmControlPanelState.ARMING"
    assert mapping["ArmState.PENDING"] == "AlarmControlPanelState.PENDING"
    for attr in ("exit_delay_remaining", "entry_delay_remaining", "arming_to"):
        assert f'"{attr}"' in src


# ---- client ingest --------------------------------------------------------

class _FakeWs:
    closed = False

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, msg: dict) -> None:
        self.sent.append(msg)


def _envelope(item: dict) -> dict:
    return {"channel": "device.helix",
            "event": {"data": {"cmdrsp": "sendMfd", "payload": [item]}}}


def test_ingest_keeps_start_for_same_version_and_notifies_once() -> None:
    client = AlulaClient()
    calls: list[int] = []
    client.on_panel_status = lambda: calls.append(1)
    client._ingest_panel_status(_status_item(), now=T0)
    first = client.panel_status
    assert first is not None and first.started_at == T0
    # requestMfd reply re-sends the cached object 29 s later.
    client._ingest_panel_status(_status_item(), now=T0 + 29)
    assert client.panel_status.started_at == T0
    assert client.panel_status.remaining(T0 + 29) == 31
    assert calls == [1]
    client._ingest_panel_status(_status_item(version=5486, exit_=False), now=T0 + 61)
    assert calls == [1, 1]
    assert not client.panel_status.in_exit_delay


def test_ws_message_routes_panel_status() -> None:
    client = AlulaClient()
    client._ingest_zone_data(_envelope(_status_item()))
    assert client.panel_status is not None and client.panel_status.in_exit_delay


@pytest.mark.asyncio
async def test_request_panel_status_sends_request_mfd() -> None:
    client = AlulaClient()
    client._panel_id = "panel-1"
    client._ws = _FakeWs()
    client._ensure_ws = AsyncMock()
    await client.request_panel_status()
    sent = client._ws.sent[-1]["send"]
    assert sent["cmdrsp"] == "requestMfd"
    assert sent["deviceId"] == "panel-1"
    assert sent["payload"] == [{"name": "panelStatus"}]


def test_connect_requests_panel_status_with_definition() -> None:
    src = (ROOT / "pyalula" / "client.py").read_text()
    block = src[src.index('{"name": "panelDefinition"}'):][:200]
    assert '{"name": "panelStatus"}' in block


# ---- redaction ------------------------------------------------------------

FAKE = [7, 7, 7, 7]


def test_redact_keypad_key_codes_and_pin_payloads() -> None:
    msg = {"event": {"data": {"cmdrsp": "sendMfd", "payload": [
        {"name": "userPin", "indexFirst": 0, "indexLast": 1,
         "items": [{"index": 0, "value": {"keypadKeyCodes": FAKE}}]},
        {"name": "duressPin", "value": {"keypadKeyCodes": FAKE},
         "lastCachedAt": "2026-11-21T23:11:46Z"},
        {"name": "zoneStatus", "items": [{"index": 0, "value": {"open": True}}]},
    ]}}}
    out = _redact(msg)
    blob = json.dumps(out)
    assert "7, 7" not in blob and "keypadKeyCodes" not in blob
    payload = out["event"]["data"]["payload"]
    assert payload[0]["items"] == "***" and payload[0]["name"] == "userPin"
    assert payload[1]["value"] == "***"
    assert payload[1]["lastCachedAt"] == "2026-11-21T23:11:46Z"
    assert payload[2]["items"][0]["value"] == {"open": True}
    assert msg["event"]["data"]["payload"][0]["items"][0]["value"]["keypadKeyCodes"] == FAKE


def test_redact_list_valued_pin_or_code_keys() -> None:
    out = _redact({"keypadKeyCodes": FAKE, "masterPinDigits": FAKE, "accessCodes": [FAKE],
                   "code": 200, "reportCode": 5, "pin": "x", "zones": [1, 2]})
    assert out["keypadKeyCodes"] == "***"
    assert out["masterPinDigits"] == "***"
    assert out["accessCodes"] == "***"
    assert out["pin"] == "***"
    assert out["code"] == 200 and out["reportCode"] == 5
    assert out["zones"] == [1, 2]
