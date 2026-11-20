"""AlulaClient — async client for the Alula alarm panel API.

Authentication:  OAuth2 password grant  →  POST /oauth/token
Panel state:     REST polling            →  GET /rest/v1/devices
Zone state:      WebSocket request/reply →  wss://api.alula.net/ws/v1
Arm / disarm:    WebSocket write         →  wss://api.alula.net/ws/v1
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import aiohttp

from .const import (
    ARM_LEVEL_AWAY,
    ARM_LEVEL_DISARM,
    ARM_LEVEL_NIGHT,
    ARM_LEVEL_STAY,
    ARM_VARIANTS,
    BASE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_CLIENT_SECRET,
    PATH_DEVICES_PANELS,
    PATH_TOKEN,
    REQUEST_TIMEOUT,
    TOKEN_REFRESH_BUFFER,
    WS_CHANNEL,
    WS_CONNECT_TIMEOUT,
    WS_RESPONSE_TIMEOUT,
    WS_URL,
    ZONE_BATCH_SIZE,
)

PATH_SELF = "/rest/v1/self"
from .models import AlarmPanel, Zone, ZoneType

_LOGGER = logging.getLogger(__name__)

# Pause between zoneBypass writes, then once more before the arm command, so the
# panel can apply each bypass before arming starts.
BYPASS_WRITE_INTERVAL = 0.25
BYPASS_BEFORE_ARM_DELAY = 1.0
# After a proven forceArm, wait before the follow-up armingLevel write that
# carries armSilent and noEntryDelay. Short enough to still be inside the
# exit window on a Concord 4.
FORCEARM_THEN_LEVEL_DELAY = 1.0

# Life-safety zones are never auto-bypassed by force arm. An explicit
# set_zone_bypass() call can still target them.
_NO_AUTO_BYPASS: frozenset[ZoneType] = frozenset({ZoneType.SMOKE, ZoneType.CO})

_SECRET_LOG_KEYS = frozenset({
    "pin",
    "password",
    "access_token",
    "refresh_token",
    "client_secret",
    "authorization",
})


def _redact(value: Any) -> Any:
    """Return a copy safe to log. Never log a PIN, password, or token."""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_LOG_KEYS:
                redacted[key] = "***"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _parse_device_type(device_type: str) -> ZoneType:
    """Map Alula deviceType strings (e.g. 'DWS') to ZoneType enum."""
    t = device_type.upper()
    if t in ("DWS", "DOOR", "WINDOW"):
        return ZoneType.DOOR
    if t in ("PIR", "MOTION", "MD"):
        return ZoneType.MOTION
    if t in ("SMOKE", "SMOKE_DET"):
        return ZoneType.SMOKE
    if t in ("CO", "CO_DET"):
        return ZoneType.CO
    if t in ("GLASS", "GB", "GLASS_BREAK"):
        return ZoneType.GLASS_BREAK
    return ZoneType.OTHER


# Map public arm mode strings → byte codes the WebSocket expects
_ARM_MODE_MAP: dict[str, int] = {
    "disarm": ARM_LEVEL_DISARM,
    "stay": ARM_LEVEL_STAY,
    "home": ARM_LEVEL_STAY,
    "night": ARM_LEVEL_NIGHT,
    "away": ARM_LEVEL_AWAY,
}

# String names used by partitionArmingLevelChange (ConnectFlex).
_PARTITION_LEVEL_NAME: dict[int, str] = {
    ARM_LEVEL_DISARM: "disarm",
    ARM_LEVEL_STAY: "stay",
    ARM_LEVEL_NIGHT: "night",
    ARM_LEVEL_AWAY: "away",
}


def _panel_reply(msg: dict) -> tuple[str | None, str | None, str]:
    """Pull request id, cmdrsp, and NAK reasons out of a panel frame."""
    if not isinstance(msg, dict):
        return None, None, ""
    nodes: list[dict] = [msg]
    event = msg.get("event")
    if isinstance(event, dict):
        data = event.get("data")
        if isinstance(data, dict):
            nodes.append(data)
            payload = data.get("payload")
            if isinstance(payload, dict):
                nodes.append(payload)
    send = msg.get("send")
    if isinstance(send, dict):
        nodes.append(send)
        payload = send.get("payload")
        if isinstance(payload, dict):
            nodes.append(payload)
    request_id: str | None = None
    cmdrsp: str | None = None
    reasons: list[str] = []
    for node in nodes:
        if request_id is None:
            raw_id = node.get("requestId", node.get("requestID"))
            if raw_id:
                request_id = str(raw_id)
        raw_cmd = node.get("cmdrsp")
        if cmdrsp is None and isinstance(raw_cmd, str):
            cmdrsp = raw_cmd.lower()
        nak = node.get("nakReasons")
        if isinstance(nak, list):
            for item in nak:
                if isinstance(item, dict):
                    reason = item.get("reason", item.get("message"))
                    if reason:
                        reasons.append(str(reason))
                elif item:
                    reasons.append(str(item))
    return request_id, cmdrsp, ", ".join(reasons)


class AlulaAuthError(Exception):
    """Invalid credentials or expired token."""


class AlulaApiError(Exception):
    """Unexpected API / network error."""


class AlulaClient:
    """Async client for the Alula alarm panel API.

    Usage::

        async with AlulaClient() as client:
            await client.login("user@example.com", "password")
            panel = await client.get_panel_status()
            print(panel.arm_state)
            await client.arm("away")
            await client.disarm()
    """

    def __init__(self, base_url: str = BASE_URL, ws_url: str = WS_URL) -> None:
        self._base_url = base_url.rstrip("/")
        self._ws_url = ws_url
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_task: asyncio.Task | None = None
        self._ws_ready: asyncio.Event = asyncio.Event()

        # Token state
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: float = 0.0
        self._user_id: str | None = None   # API user ID, needed for write commands

        # Latest zone data received from WebSocket
        self._zones: dict[str, Zone] = {}  # keyed by zone index string (0-based)
        self._zone_configs: dict[str, dict] = {}  # zone config data keyed by index

        # Previous keypad armed flag for transition detection
        self._prev_keypad_armed: bool | None = None

        # Panel metadata from panelDefinition (populated during init)
        self._max_zones: int = 64
        self._highest_zone_index: int | None = None  # from highestUsedIndexes

        # Panel id cache (filled after first get_panel_status)
        self._panel_id: str | None = None

        # Optional callback fired when keypad indicates an arm state transition
        self.on_arm_state_change: callable | None = None

        # Request ids of arm commands waiting for a panel ack or NAK.
        # Each entry is the envelope id plus the inner requestId.
        self._arm_inflight: list[set[str]] = []

    @property
    def ws_connected(self) -> bool:
        """Whether the WebSocket is currently open."""
        return self._ws is not None and not self._ws.closed

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> AlulaClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def login(self, username: str, password: str) -> None:
        """Authenticate with username/password and store the token."""
        data = await self._token_request({
            "grant_type": "password",
            "username": username,
            "password": password,
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
        })
        self._store_token(data)
        _LOGGER.debug("Alula login OK, token expires in %ds", data.get("expires_in", 0))
        # Fetch user ID needed for write commands
        self._user_id = await self._fetch_user_id()

    async def _refresh_access_token(self) -> None:
        if not self._refresh_token:
            raise AlulaAuthError("No refresh token available — call login() first")
        _LOGGER.debug("Refreshing Alula access token")
        data = await self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
        })
        self._store_token(data)

    def _store_token(self, data: dict) -> None:
        self._access_token = data["access_token"]
        expires_in: int = data.get("expires_in", 900)
        self._token_expires_at = time.monotonic() + expires_in
        self._refresh_token = data.get("refresh_token", self._refresh_token)

    async def _maybe_refresh_token(self) -> None:
        remaining = self._token_expires_at - time.monotonic()
        if remaining > TOKEN_REFRESH_BUFFER:
            return
        await self._refresh_access_token()

    async def _fetch_user_id(self) -> str | None:
        try:
            data = await self._get(self._base_url + PATH_SELF)
            uid = data.get("data", {}).get("id")
            _LOGGER.debug("Alula user ID: %s", uid)
            return uid
        except Exception as exc:
            _LOGGER.warning("Could not fetch user ID: %s", exc)
            return None

    async def _token_request(self, payload: dict) -> dict:
        url = self._base_url + PATH_TOKEN
        async with self._get_session().post(
            url,
            data=payload,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status == 400:
                body = await resp.json()
                raise AlulaAuthError(
                    f"Auth failed: {body.get('error_description', body)}"
                )
            if not resp.ok:
                raise AlulaApiError(f"Token endpoint returned {resp.status}")
            return await resp.json()

    # ------------------------------------------------------------------
    # REST: panel status
    # ------------------------------------------------------------------

    async def get_panel_status(self, panel_id: str | None = None) -> AlarmPanel:
        """Return current panel arm state via REST polling.

        Also refreshes zone list from the last WebSocket data if available.
        """
        await self._maybe_refresh_token()
        url = self._base_url + PATH_DEVICES_PANELS
        data = await self._get(url)
        items = data.get("data", [])
        if not items:
            raise AlulaApiError("No panels found on this account")

        # If caller specified a panel_id, find it; otherwise use the first
        panel_data = next(
            (d for d in items if str(d.get("id")) == panel_id),
            items[0],
        )
        self._panel_id = str(panel_data["id"])
        zones = list(self._zones.values())
        panel = AlarmPanel.from_api(panel_data, zones=zones)
        return panel

    # ------------------------------------------------------------------
    # WebSocket: connect + receive loop
    # ------------------------------------------------------------------

    async def connect_ws(self) -> None:
        """Open the WebSocket, wait for ready, then subscribe to device channels."""
        await self._maybe_refresh_token()
        if self._ws and not self._ws.closed:
            return
        if not self._panel_id:
            raise AlulaApiError("Panel ID unknown — call get_panel_status() first")

        ws_url = f"{self._ws_url}?access_token={self._access_token}"
        _LOGGER.debug("Connecting Alula WebSocket: %s", self._ws_url)
        self._ws_ready = asyncio.Event()
        self._ws = await self._get_session().ws_connect(
            ws_url,
            timeout=aiohttp.ClientWSTimeout(ws_close=WS_CONNECT_TIMEOUT),
        )
        self._ws_task = asyncio.create_task(
            self._ws_receive_loop(), name="alula-ws-recv"
        )

        # Wait for the server's "ready" message before subscribing
        await asyncio.wait_for(self._ws_ready.wait(), timeout=WS_CONNECT_TIMEOUT)

        # Subscribe to device.status, device.helix, and device.keypad
        for channel in ("device.status", "device.helix", "device.keypad"):
            await self._ws_send({
                "channel": channel,
                "id": str(uuid.uuid4()),
                "subscribe": {"deviceId": self._panel_id},
            })

        # Enable virtual keypad push stream (triggers virtualKeypadOutput events)
        await self._ws_send({
            "channel": WS_CHANNEL,
            "id": str(uuid.uuid4()),
            "send": {
                "cmdrsp": "virtualKeypadInput",
                "deviceId": self._panel_id,
                "payload": {"enable": True},
                "requestId": str(uuid.uuid4()),
            },
        })

        # Request panel definition and highest used indexes so we know zone count
        await self._ws_send({
            "channel": WS_CHANNEL,
            "id": str(uuid.uuid4()),
            "send": {
                "cmdrsp": "requestMfd",
                "deviceId": self._panel_id,
                "payload": [
                    {"name": "panelDefinition"},
                    {"name": "highestUsedIndexes"},
                ],
                "requestId": str(uuid.uuid4()),
            },
        })

        # Allow panel time to respond with definition/indexes before zone requests
        await asyncio.sleep(WS_RESPONSE_TIMEOUT)

        _LOGGER.debug("Alula WebSocket connected and subscribed")

    async def _ws_receive_loop(self) -> None:
        """Background task: read WebSocket messages and dispatch them."""
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_ws_message(msg.json())
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.warning("Alula WebSocket error: %s", self._ws.exception())
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _LOGGER.warning("Alula WebSocket receive loop died: %s", exc)
        finally:
            _LOGGER.debug("Alula WebSocket receive loop exited")

    async def _handle_ws_message(self, msg: dict) -> None:
        """Dispatch an incoming WebSocket message."""
        _LOGGER.debug("WS <<<: %s", _redact(msg))
        self._log_arm_reply(msg)

        # "ready" message — signal that subscriptions can now be sent
        if msg.get("message") == "ready" and "sessionId" in msg:
            _LOGGER.debug("WS ready, sessionId=%s", msg.get("sessionId"))
            self._ws_ready.set()
            return

        # All other messages: ingest zone/arm state data
        self._ingest_zone_data(msg)

    def _ingest_zone_data(self, msg: dict) -> None:
        """Extract and cache zone/arm state from any WS message.

        Messages from the server arrive as:
          {"channel":"device.helix","id":"...","event":{"data":{"cmdrsp":"sendMfd","payload":[...]},...}}
        or for push events:
          {"channel":"device.helix","id":"...","event":{"data":{"cmdrsp":"virtualKeypadOutput","payload":{...}},...}}
        """
        # Unwrap event envelope
        event = msg.get("event", {})
        data = event.get("data", {})
        cmdrsp = data.get("cmdrsp", "")

        # sendMfd: response to requestMfd — payload is a list of named objects
        if cmdrsp == "sendMfd":
            for item in data.get("payload", []):
                self._ingest_named_payload(item)
            return

        # virtualKeypadOutput: pushed arm/display state from the panel
        if cmdrsp == "virtualKeypadOutput":
            kp = data.get("payload", {})
            if isinstance(kp, dict):
                self._update_arm_state_from_keypad(kp)
            return

        # Fallback: some servers push zone events at the top level (older firmware)
        self._ingest_named_payload(msg.get("payload", {}))

    def _ingest_named_payload(self, item: dict) -> None:
        """Parse a single payload entry from a sendMfd response.

        Entries use the items[] format:
          {"name": "zoneStatus", "indexFirst": 0, "indexLast": N,
           "items": [{"index": 0, "value": {...}}, ...]}
        """
        if not isinstance(item, dict):
            return
        name = item.get("name", "")

        if name == "zoneConfiguration":
            for entry in item.get("items", []):
                idx = str(entry.get("index", "?"))
                cfg = entry.get("value", {})
                if not cfg:
                    continue
                self._zone_configs[idx] = cfg
                # Propagate name/type to existing zone object
                if idx in self._zones:
                    self._zones[idx].name = cfg.get("zoneName", self._zones[idx].name)
                    self._zones[idx].zone_type = _parse_device_type(cfg.get("deviceType", ""))

        elif name == "zoneStatus":
            for entry in item.get("items", []):
                idx = str(entry.get("index", "?"))
                value = entry.get("value", {})
                if not value:
                    continue
                cfg = self._zone_configs.get(idx, {})
                if idx not in self._zones:
                    zone_name = cfg.get("zoneName") or f"Zone {int(idx) + 1:02d}"
                    self._zones[idx] = Zone(
                        id=idx,
                        name=zone_name,
                        zone_type=_parse_device_type(cfg.get("deviceType", "")),
                        is_open=False,
                    )
                self._zones[idx].is_open = bool(value.get("open", False))
                self._zones[idx].is_bypassed = bool(value.get("bypassed", False))
                self._zones[idx].raw = value

        elif name == "panelDefinition":
            value = item.get("value", {})
            max_zones = value.get("maxZones")
            if max_zones:
                self._max_zones = int(max_zones)
                _LOGGER.debug("Panel maxZones=%d", self._max_zones)

        elif name == "highestUsedIndexes":
            value = item.get("value", {})
            # May be under "value" key directly or as a named field
            zone_idx = value.get("zoneIndex", value.get("zone"))
            if zone_idx is not None:
                self._highest_zone_index = int(zone_idx)
                _LOGGER.debug("Highest used zone index=%d", self._highest_zone_index)

        elif name in ("zoneUpdate", "zoneFault", "zoneRestore"):
            # Single-zone push (legacy flat format)
            value = item.get("value", item)
            try:
                zone = Zone.from_ws(value if isinstance(value, dict) else item)
                self._zones[zone.id] = zone
            except Exception:
                pass

    def _update_arm_state_from_keypad(self, kp: dict) -> None:
        """Detect arm-state transitions from virtualKeypadOutput and request a REST refresh.

        The keypad push is NOT used as authoritative state — REST ``armingLevel``
        is the sole source of truth.  We only watch for transitions (armed flag
        changing, or triggered indicators) and fire the callback so the HA
        coordinator triggers an immediate REST poll.
        """
        _LOGGER.debug("virtualKeypadOutput payload: %s", kp)
        armed = kp.get("armed", False)
        fire = kp.get("fire", False)
        alarm_memory = kp.get("alarmMemory", False)
        triggered = fire or alarm_memory

        # Detect transitions only — fire callback so coordinator does a REST poll
        prev = self._prev_keypad_armed
        self._prev_keypad_armed = armed

        if prev is not None and (armed != prev or triggered):
            _LOGGER.debug(
                "Keypad transition detected (armed: %s → %s, triggered: %s), requesting refresh",
                prev, armed, triggered,
            )
            if self.on_arm_state_change:
                self.on_arm_state_change()

    # ------------------------------------------------------------------
    # WebSocket: zone status request
    # ------------------------------------------------------------------

    async def fetch_zone_statuses(self, device_id: str | None = None) -> list[Zone]:
        """Request zone statuses over WebSocket and return the result.

        Requests zoneConfiguration+zoneStatus in batches of 3 (matching the
        web app's behaviour) so the panel firmware doesn't drop large responses.
        """
        dev_id = device_id or self._panel_id
        if not dev_id:
            raise AlulaApiError("Panel ID unknown — call get_panel_status() first")

        await self._ensure_ws()

        # Determine range to request
        max_index = self._highest_zone_index if self._highest_zone_index is not None \
            else self._max_zones - 1

        # Send batched requests (3 zones per batch, matching the web app)
        for first in range(0, max_index + 1, ZONE_BATCH_SIZE):
            last = min(first + ZONE_BATCH_SIZE - 1, max_index)
            inner = {
                "deviceId": dev_id,
                "cmdrsp": "requestMfd",
                "payload": [
                    {"name": "zoneConfiguration", "indexFirst": first, "indexLast": last},
                    {"name": "zoneStatus", "indexFirst": first, "indexLast": last},
                ],
                "requestId": str(uuid.uuid4()),
            }
            await self._ws_send({"channel": WS_CHANNEL, "id": str(uuid.uuid4()), "send": inner})

        # Wait for panel responses (arrive as pushed sendMfd events)
        await asyncio.sleep(WS_RESPONSE_TIMEOUT)

        return list(self._zones.values())

    # ------------------------------------------------------------------
    # WebSocket: arm / disarm
    # ------------------------------------------------------------------

    async def arm(self, mode: str, user_number: int = 0,
                  silent: bool = False, no_entry_delay: bool = False,
                  device_id: str | None = None) -> None:
        """Arm the panel.

        :param mode: 'away', 'home'/'stay', or 'night'
        :param user_number: helix user index (0 = master)
        :param silent: arm without sounding exit beeps
        :param no_entry_delay: bypass entry delay
        """
        level = self._arm_level(mode)
        await self._send_arming_level(
            level, user_number, silent, no_entry_delay, device_id
        )

    async def disarm(self, pin: str | list[str],
                     silent: bool = False, no_entry_delay: bool = False,
                     device_id: str | None = None) -> None:
        """Disarm the panel.

        :param pin: user PIN code as a string ('1234') or list of digit strings (['1','2','3','4'])
        """
        dev_id = device_id or self._panel_id
        if not dev_id:
            raise AlulaApiError("Panel ID unknown — call get_panel_status() first")

        await self._ensure_ws()

        pin_digits = list(pin) if isinstance(pin, str) else [str(d) for d in pin]

        inner = {
            "deviceId": dev_id,
            "cmdrsp": "changeArmingLevelUsingCode",
            "payload": {
                "armingLevelValue": ARM_LEVEL_DISARM,
                "armSilent": silent,
                "noEntryDelay": no_entry_delay,
                "pin": pin_digits,
            },
            "requestId": str(uuid.uuid4()),
        }
        msg = {"channel": WS_CHANNEL, "id": str(uuid.uuid4()), "send": inner}
        await self._ws_send(msg)
        _LOGGER.debug("Sent changeArmingLevelUsingCode (disarm) to panel %s", dev_id)

    async def _send_arming_level(
        self,
        level: int,
        user_number: int,
        silent: bool,
        no_entry_delay: bool,
        device_id: str | None,
    ) -> None:
        dev_id = device_id or self._panel_id
        if not dev_id:
            raise AlulaApiError("Panel ID unknown — call get_panel_status() first")

        await self._ensure_ws()

        inner = {
            "deviceId": dev_id,
            "cmdrsp": "writeMfd",
            "payload": [{
                "name": "armingLevel",
                "value": {
                    "armingLevelValue": level,
                    "userNumber": user_number,
                    "armSilent": silent,
                    "noEntryDelay": no_entry_delay,
                },
            }],
            "requestId": self._user_id or str(uuid.uuid4()),
        }
        msg = {"channel": WS_CHANNEL, "id": str(uuid.uuid4()), "send": inner}
        await self._ws_send(msg, arm_trace=True)

    async def force_arm(
        self,
        mode: str,
        user_number: int = 0,
        device_id: str | None = None,
        *,
        silent: bool = False,
        no_entry_delay: bool = False,
    ) -> None:
        """Arm with the panel's native ``forceArm`` write. No PIN is sent.

        On a Concord 4 with BAT-Connect this is the command that arms when
        zones are open. Bypassing zones and then sending ``armingLevel`` does
        not: the keypad stays on the indirect-bypass prompt.

        Payload, confirmed against cove-alula 1.4.0 ``async_force_arm``::

            [{"name": "forceArm",
              "value": {"armingLevelValue": N, "userNumber": 0}}]

        Levels match this client's existing map: 2 home/stay, 3 night, 4 away.

        ``armSilent`` and ``noEntryDelay`` are added only when one of them is
        true. No captured command shows those keys on ``forceArm``; that
        shape is unverified on a Concord 4. A default call keeps the payload
        that armed a Concord 4.
        """
        await self._send_force_arm(
            mode,
            user_number=user_number,
            device_id=device_id,
            silent=silent,
            no_entry_delay=no_entry_delay,
            include_flags=bool(silent or no_entry_delay),
        )

    async def arm_with_variant(
        self,
        mode: str,
        variant: str,
        user_number: int = 0,
        device_id: str | None = None,
        *,
        silent: bool = False,
        no_entry_delay: bool = False,
    ) -> None:
        """Send one no-PIN arm shape so the payload can be live-tested.

        ``forcearm`` puts the flags on the forceArm value.
        ``arminglevel`` is the existing armingLevel write.
        ``partition`` is partitionArmingLevelChange with authType user.
        ``forcearm_then_arminglevel`` sends the proven forceArm, waits, then
        writes armingLevel with the flags.

        None of these send or store a PIN. The flags are unverified on a
        Concord 4. Concord 4 has no night level: pass mode ``home`` (stay)
        for the silent no-delay stay experiment.
        """
        kind = variant.strip().lower()
        if kind not in ARM_VARIANTS:
            raise ValueError(
                f"Invalid arm variant {variant!r}. Use: {', '.join(ARM_VARIANTS)}"
            )
        if kind == "forcearm":
            await self._send_force_arm(
                mode,
                user_number=user_number,
                device_id=device_id,
                silent=silent,
                no_entry_delay=no_entry_delay,
                include_flags=True,
            )
            return
        if kind == "arminglevel":
            await self.arm(
                mode,
                user_number=user_number,
                silent=silent,
                no_entry_delay=no_entry_delay,
                device_id=device_id,
            )
            return
        if kind == "partition":
            await self._send_partition_arm(
                mode,
                user_number=user_number,
                device_id=device_id,
                silent=silent,
                no_entry_delay=no_entry_delay,
            )
            return
        await self._send_force_arm(
            mode,
            user_number=user_number,
            device_id=device_id,
            silent=False,
            no_entry_delay=False,
            include_flags=False,
        )
        await asyncio.sleep(FORCEARM_THEN_LEVEL_DELAY)
        await self.arm(
            mode,
            user_number=user_number,
            silent=silent,
            no_entry_delay=no_entry_delay,
            device_id=device_id,
        )

    async def _send_force_arm(
        self,
        mode: str,
        user_number: int,
        device_id: str | None,
        silent: bool,
        no_entry_delay: bool,
        include_flags: bool,
    ) -> None:
        level = self._arm_level(mode)
        value: dict[str, Any] = {
            "armingLevelValue": level,
            "userNumber": int(user_number),
        }
        if include_flags:
            value["armSilent"] = bool(silent)
            value["noEntryDelay"] = bool(no_entry_delay)
        await self._send_write_mfd(
            [{"name": "forceArm", "value": value}],
            device_id,
            arm_trace=True,
        )

    async def _send_partition_arm(
        self,
        mode: str,
        user_number: int,
        device_id: str | None,
        silent: bool,
        no_entry_delay: bool,
    ) -> None:
        """partitionArmingLevelChange, authType user, no PIN.

        Payload matches the ConnectFlex arm in cove-alula
        ``_build_arming_command`` (kind ``partition``), including
        ``forceArm: false`` inside that payload. Unverified on a Concord 4.
        """
        level = self._arm_level(mode)
        name = _PARTITION_LEVEL_NAME.get(level)
        if name is None:
            raise ValueError(f"Partition arming does not support level {level}")
        dev_id = device_id or self._panel_id
        if not dev_id:
            raise AlulaApiError("Panel ID unknown — call get_panel_status() first")
        await self._ensure_ws()
        inner = {
            "deviceId": dev_id,
            "cmdrsp": "partitionArmingLevelChange",
            "payload": {
                "armingLevel": name,
                "partitions": [True, False, False, False, False, False, False, False],
                "armSilent": bool(silent),
                "noEntryDelay": bool(no_entry_delay),
                "authType": "user",
                "userNumber": int(user_number),
                "forceArm": False,
            },
            "requestId": str(uuid.uuid4()),
        }
        msg = {"channel": WS_CHANNEL, "id": str(uuid.uuid4()), "send": inner}
        await self._ws_send(msg, arm_trace=True)

    async def set_zone_bypass(
        self,
        zone_index: int,
        bypass: bool = True,
        user_number: int = 0,
        device_id: str | None = None,
    ) -> None:
        """Bypass or restore one zone. No PIN is sent.

        Payload, confirmed against cove-alula 1.4.0 ``async_set_zone_bypass``::

            {"name": "zoneBypass"|"zoneUnbypass",
             "indexFirst": i, "indexLast": i,
             "items": [{"index": i, "value": {"userNumber": 0}}]}

        ``zone_index`` is the 0-based Alula MFD index.
        """
        if isinstance(zone_index, bool) or not isinstance(zone_index, int):
            raise TypeError(f"zone_index must be an int, got {zone_index!r}")
        if zone_index < 0:
            raise ValueError(f"zone_index must be >= 0, got {zone_index}")
        name = "zoneBypass" if bypass else "zoneUnbypass"
        await self._send_write_mfd(
            [{
                "name": name,
                "indexFirst": zone_index,
                "indexLast": zone_index,
                "items": [{
                    "index": zone_index,
                    "value": {"userNumber": int(user_number)},
                }],
            }],
            device_id,
        )
        _LOGGER.debug("Sent %s for zone %d", name, zone_index)

    async def arm_bypassing_open(
        self,
        mode: str,
        user_number: int = 0,
        device_id: str | None = None,
        *,
        refresh_zones: bool = True,
        silent: bool = False,
        no_entry_delay: bool = False,
    ) -> list[int]:
        """Bypass currently open zones, then arm with the existing no-PIN arm.

        On a Concord 4 with BAT-Connect this does not bypass the zones. The
        panel stays disarmed and the keypad stays on the indirect-bypass
        prompt. Use ``force_arm`` instead.

        Smoke and CO zones are left alone. Every other open zone is sent
        ``zoneBypass``, including zones the cloud already marks bypassed,
        because a Concord keypad can still be waiting on its bypass prompt
        after the cloud has flipped that flag. Returns the indexes written.
        """
        self._arm_level(mode)  # validate before any bypass is sent
        dev_id = device_id or self._panel_id
        if not dev_id:
            raise AlulaApiError("Panel ID unknown — call get_panel_status() first")

        if refresh_zones:
            try:
                await self.fetch_zone_statuses(device_id=dev_id)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning(
                    "Zone refresh before force arm failed (%s); using the last known zone state",
                    err,
                )

        indexes = self.open_bypassable_zone_indexes()
        for offset, index in enumerate(indexes):
            if offset:
                await asyncio.sleep(BYPASS_WRITE_INTERVAL)
            await self.set_zone_bypass(
                index, True, user_number=user_number, device_id=dev_id
            )
        if indexes:
            _LOGGER.info(
                "Bypassed open zone(s) %s before arming %s",
                indexes,
                mode,
            )
            await asyncio.sleep(BYPASS_BEFORE_ARM_DELAY)
        else:
            _LOGGER.info("No open zones to bypass before arming %s", mode)
        await self.arm(
            mode,
            user_number=user_number,
            silent=silent,
            no_entry_delay=no_entry_delay,
            device_id=dev_id,
        )
        return indexes

    def open_bypassable_zone_indexes(self) -> list[int]:
        """0-based indexes of open zones that force arm may bypass."""
        found: list[int] = []
        for zone in self._zones.values():
            if not zone.is_open:
                continue
            if zone.zone_type in _NO_AUTO_BYPASS:
                _LOGGER.warning(
                    "Not auto-bypassing %s zone %s (%s)",
                    zone.zone_type.value,
                    zone.id,
                    zone.name,
                )
                continue
            try:
                found.append(int(zone.id))
            except (TypeError, ValueError):
                _LOGGER.warning("Skipping zone with non-numeric id %r", zone.id)
        return sorted(set(found))

    def _arm_level(self, mode: str) -> int:
        level = _ARM_MODE_MAP.get(mode.lower())
        if level is None:
            raise ValueError(
                f"Invalid arm mode {mode!r}. Use: away, stay/home, night"
            )
        return level

    async def _send_write_mfd(
        self,
        payload: list[dict],
        device_id: str | None,
        *,
        arm_trace: bool = False,
    ) -> None:
        """Send a writeMfd command on the device.helix channel."""
        dev_id = device_id or self._panel_id
        if not dev_id:
            raise AlulaApiError("Panel ID unknown — call get_panel_status() first")
        await self._ensure_ws()
        inner = {
            "deviceId": dev_id,
            "cmdrsp": "writeMfd",
            "bypassCache": True,
            "payload": payload,
            "requestId": self._user_id or str(uuid.uuid4()),
        }
        msg = {"channel": WS_CHANNEL, "id": str(uuid.uuid4()), "send": inner}
        await self._ws_send(msg, arm_trace=arm_trace)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            )
        return self._session

    def _auth_headers(self) -> dict[str, str]:
        if not self._access_token:
            raise AlulaAuthError("Not authenticated — call login() first")
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _get(self, url: str) -> Any:
        headers = {**self._auth_headers(), "Accept": "application/json"}
        async with self._get_session().get(url, headers=headers) as resp:
            return await self._handle_response(resp)

    async def _ensure_ws(self) -> None:
        """Connect WebSocket if not already open."""
        await self._maybe_refresh_token()
        if self._ws is None or self._ws.closed:
            await self.connect_ws()

    def _arm_ids(self, msg: dict) -> set[str]:
        ids: set[str] = set()
        envelope_id = msg.get("id")
        if envelope_id:
            ids.add(str(envelope_id))
        send = msg.get("send")
        if isinstance(send, dict) and send.get("requestId"):
            ids.add(str(send["requestId"]))
        return ids

    def _note_arm_command(self, msg: dict) -> None:
        ids = self._arm_ids(msg)
        if not ids:
            return
        self._arm_inflight.append(ids)
        if len(self._arm_inflight) > 20:
            del self._arm_inflight[:-20]

    def _drop_arm_command(self, msg: dict) -> None:
        ids = self._arm_ids(msg)
        for group in self._arm_inflight:
            if group & ids:
                self._arm_inflight.remove(group)
                return

    def _log_arm_reply(self, msg: dict) -> None:
        """Log a panel ack or NAK for an arm command at INFO. Never a PIN."""
        if not self._arm_inflight:
            return
        request_id, cmdrsp, reasons = _panel_reply(msg)
        if cmdrsp not in ("ack", "nak"):
            return
        if request_id:
            matched = False
            for group in self._arm_inflight:
                if request_id in group:
                    self._arm_inflight.remove(group)
                    matched = True
                    break
            if not matched:
                return
        else:
            self._arm_inflight.pop(0)
        redacted = _redact(msg)
        if reasons:
            _LOGGER.info(
                "Alula panel %s requestId=%s reasons=%s: %s",
                cmdrsp.upper(),
                request_id or "",
                reasons,
                redacted,
            )
            return
        _LOGGER.info(
            "Alula panel %s requestId=%s: %s",
            cmdrsp.upper(),
            request_id or "",
            redacted,
        )

    async def _ws_send(self, msg: dict, *, arm_trace: bool = False) -> None:
        if self._ws is None or self._ws.closed:
            raise AlulaApiError("WebSocket not connected")
        redacted = _redact(msg)
        if arm_trace:
            # Record the id and log before the await. The receive loop can run
            # while send_json yields, and the ack can arrive before we resume.
            self._note_arm_command(msg)
            _LOGGER.info("Alula WS command: %s", redacted)
        else:
            _LOGGER.debug("WS >>>: %s", redacted)
        try:
            await self._ws.send_json(msg)
        except Exception:
            if arm_trace:
                self._drop_arm_command(msg)
            raise

    @staticmethod
    async def _handle_response(resp: aiohttp.ClientResponse) -> Any:
        if resp.status == 401:
            raise AlulaAuthError("Alula API returned 401 — token invalid or expired")
        if not resp.ok:
            text = await resp.text()
            raise AlulaApiError(f"Alula API {resp.status}: {text[:200]}")
        return await resp.json()
