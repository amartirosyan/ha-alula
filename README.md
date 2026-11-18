# Alula for Home Assistant

A [Home Assistant](https://www.home-assistant.io/) custom integration for [Alula](https://www.alula.com/) security alarm panels (Connect+, Helix, Concord 4 with BAT-Connect, and others that use the Alula cloud).

This repository is [amartirosyan/ha-alula](https://github.com/amartirosyan/ha-alula), a fork of [pschorf/ha-alula](https://github.com/pschorf/ha-alula).

## Features

- **Alarm control panel** entity with arm away, arm home, arm night, and disarm
- **Force arm** when doors or windows are open, without storing a PIN
- **Zone bypass** for a single zone, from the zone sensor or its 0-based index
- **Binary sensor** entities for each zone (doors, motion, glass break, smoke, CO), including the `bypassed` attribute
- Near-instant state updates via WebSocket push events
- PIN code support for disarming (entered when you disarm; not saved for arming)

Arm Home, Arm Away, Arm Night, and Arm custom bypass send the panel's native `forceArm` command and do not use a PIN. On a Concord 4 with BAT-Connect that starts the arming countdown and marks open zones bypassed. Home Assistant can take 30-90 seconds to show the new state.

## Installation

### HACS (custom repository)

1. In Home Assistant, open **HACS → Integrations**.
2. Open the three-dot menu and choose **Custom repositories**.
3. Repository: `https://github.com/amartirosyan/ha-alula`
4. Category: **Integration**. Add it.
5. Find **Alula** in the HACS integration list and install it.
6. Restart Home Assistant.

HACS installs the latest GitHub release when this repository has one. Version 0.2.1 is the release that arms a Concord 4 with `forceArm`.

### Manual

Copy the `custom_components/alula` directory into your Home Assistant `custom_components/` folder and restart. The `pyalula` client is vendored inside that directory; do not install `pyalula` from PyPI for this integration.

## Setup

After installation, go to **Settings > Devices & Services > Add Integration > Alula** and enter:

- **Email** -- your Alula account email
- **Password** -- your Alula account password
- **Panel ID** -- leave blank unless you have multiple panels on one account

Disarm still asks for the panel PIN at the moment you disarm. Arming, force arm, and zone bypass do not use or store that PIN.

## Force arm and zone bypass

Call these from **Developer tools → Actions**, an automation, or a script.

Force arm Home. `method` defaults to `native` (`forceArm`, no PIN):

```yaml
action: alula.force_arm
data:
  entity_id: alarm_control_panel.closet_4508_ethel_ave
  mode: home
  method: native
```

`mode` is `home`, `away`, or `night`. `method: bypass` writes `zoneBypass` for each open zone and then arms. On a Concord 4 that does not bypass the zones: the panel stays disarmed and the keypad stays on the indirect-bypass prompt. Leave `method` at `native`.

Bypass one zone from its binary sensor:

```yaml
action: alula.bypass_zone
data:
  entity_id: binary_sensor.kitchen_window
  bypass: true
```

Or by the 0-based Alula zone index (the number in the entity unique id `alula_zone_<index>`, which is not the keypad's 1-based zone number):

```yaml
action: alula.bypass_zone
data:
  zone: 4
  bypass: false
```

With more than one Alula panel configured, add `entity_id` of that panel when you pass a zone index.

`alarm_control_panel.alarm_arm_custom_bypass` and the Arm Home, Arm Away, and Arm Night buttons send the same native `forceArm` command.

## Manual check

1. Open one window and leave the other doors and motion sensors closed.
2. Arm Home from the alarm panel card, or run `alula.force_arm` for `alarm_control_panel.closet_4508_ethel_ave` with `mode: home` (method `native`, or omit method).
3. The keypad should go straight to the arming countdown and should not sit on "press # to bypass".
4. Within about 30-90 seconds, confirm the alarm panel state is `armed_home` and the open zones show `bypassed: true`.
5. Disarm with the keypad, or with the alarm panel disarm action and the PIN typed into that call. Confirm the zone's `bypassed` attribute clears.

## pyalula

The integration vendors its own copy of the Alula client at `custom_components/alula/pyalula` (version 0.2.1). Home Assistant does not install `pyalula` from PyPI.

The same client still lives at the repository root for local use:

```python
from pyalula import AlulaClient

async with AlulaClient() as client:
    await client.login("user@example.com", "password")
    panel = await client.get_panel_status()
    print(panel.arm_state)
    await client.force_arm("away")
```

## Development

```bash
pip install -e ".[dev]"
pytest

# Library smoke test (requires real credentials):
ALULA_USER=you@example.com ALULA_PASS=secret python -m pyalula.smoke_test
```

## Disclaimer

This project is not affiliated with or endorsed by Alula. It uses the same API as the official Alula mobile app. Use at your own risk. Arming a monitored system can notify the central station.

## License

MIT
