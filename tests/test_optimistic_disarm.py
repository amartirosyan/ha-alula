"""Optimistic disarm on panel ACK and command_in_progress. No panel, no PIN.

The coordinator is loaded against a tiny stand-in for the Home Assistant
pieces it uses, so this runs without Home Assistant installed.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pyalula.client import AlulaClient
from pyalula.models import AlarmPanel, ArmState, PanelStatus

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "alula"


# ---- HA stand-in -----------------------------------------------------------

def _install_ha_stub() -> None:
    existing = sys.modules.get("homeassistant")
    if existing is not None:
        if getattr(existing, "_alula_stub", False):
            return
        pytest.skip("real Home Assistant loaded; stub tests not applicable")
    if importlib.util.find_spec("homeassistant") is not None:
        pytest.skip("real Home Assistant installed; stub tests not applicable")
    ha = types.ModuleType("homeassistant")
    ha._alula_stub = True  # type: ignore[attr-defined]
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda f: f
    helpers = types.ModuleType("homeassistant.helpers")
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")

    class UpdateFailed(Exception):
        pass

    class DataUpdateCoordinator:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, hass, logger, name, update_interval) -> None:
            self.hass = hass
            self.data = None
            self.updates = 0

        def async_update_listeners(self) -> None:
            self.updates += 1

        def async_set_updated_data(self, data) -> None:
            self.data = data
            self.async_update_listeners()

        async def async_refresh(self) -> None:
            self.data = await self._async_update_data()
            self.async_update_listeners()

        async def async_request_refresh(self) -> None:
            await self.async_refresh()

        async def async_shutdown(self) -> None:
            return None

    uc.DataUpdateCoordinator = DataUpdateCoordinator
    uc.UpdateFailed = UpdateFailed
    sys.modules.update({
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.update_coordinator": uc,
    })


def _load_coordinator():
    _install_ha_stub()
    pkg = types.ModuleType("alula_t")
    pkg.__path__ = [str(COMPONENT)]  # type: ignore[attr-defined]
    sys.modules.setdefault("alula_t", pkg)
    return importlib.import_module("alula_t.coordinator")


class _Hass:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()

    def async_create_task(self, coro):
        coro.close()  # tests never run background refreshes
        return None


class _FakeWs:
    closed = False

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, msg: dict) -> None:
        self.sent.append(msg)


def _client() -> AlulaClient:
    c = AlulaClient()
    c._panel_id = "panel-1"
    c._user_id = "user-1"
    c._ws = _FakeWs()
    c._ensure_ws = AsyncMock()
    return c


def _panel(level: str) -> AlarmPanel:
    return AlarmPanel.from_api(
        {"id": "panel-1", "attributes": {"friendlyName": "Home", "armingLevel": level}}
    )


def _reply(request_id: str, cmdrsp: str, reasons=None) -> dict:
    data = {"cmdrsp": cmdrsp, "requestId": request_id}
    if reasons:
        data["nakReasons"] = [{"reason": r} for r in reasons]
    return {"channel": "device.helix", "event": {"data": data}}


FAKE_PIN = "0000"  # not a real code


# ---- client ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_disarm_ack_and_nak_fire_callback_arm_ack_does_not() -> None:
    c = _client()
    got: list[tuple[str, str]] = []
    c.on_disarm_reply = lambda cmd, reasons: got.append((cmd, reasons))
    await c.disarm(pin=FAKE_PIN)
    rid = c._ws.sent[-1]["send"]["requestId"]
    await c._handle_ws_message(_reply(rid, "ack"))
    assert got == [("ack", "")]
    await c.disarm(pin=FAKE_PIN)
    rid2 = c._ws.sent[-1]["send"]["requestId"]
    await c._handle_ws_message(_reply(rid2, "nak", ["Invalid User Number"]))
    assert got[-1] == ("nak", "Invalid User Number")
    # An arm-level write's ACK is not a disarm reply.
    await c._send_arming_level(2, 0, False, False, None)
    await c._handle_ws_message(_reply("user-1", "ack"))
    assert len(got) == 2
    # The PIN never appears in the tracked ids.
    assert all(FAKE_PIN not in i for i in c._disarm_ids)


def test_newer_disarmed_status_beats_older_rest() -> None:
    p = _panel("stay")
    p.rest_fetched_at = 100.0
    st = PanelStatus.from_payload(
        {"name": "panelStatus", "value": {"armingLevel": 1, "statusVersion": 9}}, 105.0
    )
    assert p.apply_status(st, 106.0) == ArmState.DISARMED
    p2 = _panel("stay")
    p2.rest_fetched_at = 110.0  # REST read after that status: REST wins
    assert p2.apply_status(st, 111.0) == ArmState.ARMED_HOME


# ---- hold and tracker (HA-free) ---------------------------------------------

def _fast():
    return _load_coordinator().__dict__  # ensure stub; fast_refresh via package


def test_optimistic_hold_rules() -> None:
    fr = importlib.import_module("alula_t.fast_refresh") if _fast() else None
    t = {"now": 0.0}
    h = fr.OptimisticHold(hold=15, clock=lambda: t["now"])
    assert h.apply("stay") == "stay"
    h.set("disarm", marker=5)
    t["now"] = 4
    assert h.apply("stay", marker=5) == "disarm"   # REST lagging: hold
    assert h.apply("disarm", marker=5) == "disarm"  # REST agrees: released
    assert not h.active
    h.set("disarm", marker=5)
    t["now"] = 20
    assert h.apply("stay", marker=5) == "stay"      # hold over: REST wins
    h.set("disarm", marker=5)
    assert h.apply("stay", marker=6) == "stay"      # newer panel status wins


def test_command_tracker_timeout() -> None:
    fr = importlib.import_module("alula_t.fast_refresh") if _fast() else None
    t = {"now": 0.0}
    c = fr.CommandTracker(timeout=20, clock=lambda: t["now"])
    c.begin("disarming")
    assert c.current == "disarming"
    assert not c.end("arming") and c.current == "disarming"
    t["now"] = 20.5
    assert c.current is None
    c.begin("arming")
    assert c.end() and c.current is None


# ---- coordinator -------------------------------------------------------------

def _coordinator(client: AlulaClient):
    mod = _load_coordinator()
    coord = mod.AlulaCoordinator(_Hass(), client, panel_id="panel-1", scan_interval=30)
    coord._fast.trigger = lambda: None  # no background burst in tests
    return coord


@pytest.mark.asyncio
async def test_ack_sets_disarmed_now_then_rest_confirms() -> None:
    c = _client()
    coord = _coordinator(c)
    coord.data = _panel("stay")
    coord.async_begin_command("disarming")
    assert coord.command_in_progress == "disarming"
    await c.disarm(pin=FAKE_PIN)
    rid = c._ws.sent[-1]["send"]["requestId"]
    before = coord.updates
    await c._handle_ws_message(_reply(rid, "ack"))
    assert coord.data.arm_state == ArmState.DISARMED
    assert coord.command_in_progress is None
    assert coord.updates > before
    # Burst baseline reads REST, so the burst keeps going until REST agrees.
    assert coord._arm_state() == ArmState.ARMED_HOME

    # A lagging REST read inside the hold keeps disarmed.
    c.get_panel_status = AsyncMock(return_value=_panel("stay"))
    c.request_panel_status = AsyncMock()
    c.fetch_zone_statuses = AsyncMock(return_value=[])
    c._ws.closed = False
    type(c).ws_connected = property(lambda self: True)
    panel = await coord._async_update_data()
    assert panel.arm_state == ArmState.DISARMED

    # REST agrees: released, real state from then on.
    c.get_panel_status = AsyncMock(return_value=_panel("disarm"))
    assert (await coord._async_update_data()).arm_state == ArmState.DISARMED
    assert not coord._hold.active
    c.get_panel_status = AsyncMock(return_value=_panel("stay"))
    assert (await coord._async_update_data()).arm_state == ArmState.ARMED_HOME
    coord._command_timer and coord._command_timer.cancel()


@pytest.mark.asyncio
async def test_nak_clears_command_and_keeps_state() -> None:
    c = _client()
    coord = _coordinator(c)
    coord.data = _panel("stay")
    coord.async_begin_command("disarming")
    await c.disarm(pin=FAKE_PIN)
    rid = c._ws.sent[-1]["send"]["requestId"]
    await c._handle_ws_message(_reply(rid, "nak", ["Invalid User Number"]))
    assert coord.command_in_progress is None
    assert coord.data.arm_state == ArmState.ARMED_HOME
    assert not coord._hold.active


@pytest.mark.asyncio
async def test_command_timeout_clears(monkeypatch) -> None:
    c = _client()
    mod = _load_coordinator()
    monkeypatch.setattr(mod, "COMMAND_TIMEOUT", 0.01)
    coord = mod.AlulaCoordinator(_Hass(), c, panel_id="panel-1", scan_interval=30)
    coord._command = importlib.import_module("alula_t.fast_refresh").CommandTracker(timeout=0.01)
    coord.async_begin_command("disarming")
    before = coord.updates
    await asyncio.sleep(0.25)
    assert coord.command_in_progress is None
    assert coord._command_timer is None
    assert coord.updates > before  # state rewritten when it times out


def test_entity_exposes_command_in_progress() -> None:
    src = (COMPONENT / "alarm_control_panel.py").read_text()
    assert '"command_in_progress": self.coordinator.command_in_progress' in src
    assert 'async_begin_command("disarming")' in src
    assert src.count("await self._arming(") == 4


@pytest.mark.asyncio
async def test_disarm_push_after_ack_keeps_hold_against_stale_rest() -> None:
    c = _client()
    coord = _coordinator(c)
    coord.data = _panel("stay")
    c._ingest_panel_status(
        {"name": "panelStatus", "value": {"armingLevel": 2, "statusVersion": 10}}
    )
    coord.async_begin_command("disarming")
    await c.disarm(pin=FAKE_PIN)
    rid = c._ws.sent[-1]["send"]["requestId"]
    await c._handle_ws_message(_reply(rid, "ack"))
    # Panel then pushes a disarmed panelStatus (new version).
    c._ingest_panel_status(
        {"name": "panelStatus", "value": {"armingLevel": 1, "statusVersion": 11}}
    )
    assert coord.data.arm_state == ArmState.DISARMED
    # A stale REST read afterwards does not flip it back inside the hold.
    stale = _panel("stay")
    stale.rest_fetched_at = 10**10
    stale.apply_status(c.panel_status, 10**10)
    assert coord._apply_hold(stale).arm_state == ArmState.DISARMED
    # A newer status saying armed releases the hold.
    c._ingest_panel_status(
        {"name": "panelStatus", "value": {"armingLevel": 2, "statusVersion": 12}}
    )
    assert coord.data.arm_state == ArmState.ARMED_HOME
    coord._command_timer and coord._command_timer.cancel()
