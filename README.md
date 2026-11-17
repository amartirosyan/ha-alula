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

The Arm Home, Arm Away, and Arm Night buttons still send the original no-PIN `armingLevel` write. That path can leave a Concord 4 keypad on its "press # to bypass" prompt when a zone is open. Use **Force arm** (or the panel's **Arm custom bypass** button, which force-arms Home) so open zones are bypassed before that arm command.

## Installation

### HACS (custom repository)

1. In Home Assistant, open **HACS → Integrations**.
2. Open the three-dot menu and choose **Custom repositories**.
3. Repository: `https://github.com/amartirosyan/ha-alula`
4. Category: **Integration**. Add it.
5. Find **Alula** in the HACS integration list and install it.
6. Restart Home Assistant.

HACS installs the latest GitHub release when this repository has one, and the `master` branch otherwise. Version 0.2.0 is the release that adds force arm.

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

Force arm Home. `method` defaults to `bypass` (bypass every currently open zone, except smoke and CO, then arm):

```yaml
action: alula.force_arm
data:
  entity_id: alarm_control_panel.closet_4508_ethel_ave
  mode: home
  method: bypass
```

`mode` is `home`, `away`, or `night`. `method: native` sends the panel `forceArm` command instead of bypassing zones first.

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

`alarm_control_panel.alarm_arm_custom_bypass` on the panel entity does the same thing as force arm Home with `method: bypass`.

## Manual check

1. Open one window and leave the other doors and motion sensors closed.
2. Run `alula.force_arm` for `alarm_control_panel.closet_4508_ethel_ave` with `mode: home` (method `bypass`, or omit method).
3. Confirm that window's binary sensor `bypassed` attribute is on, the alarm panel state is `armed_home`, and the Concord 4 keypad is armed and is not stuck on "press # to bypass".
4. Disarm with the keypad, or with the alarm panel disarm action and the PIN typed into that call. Confirm the zone's `bypassed` attribute clears.

`method: native` is the fallback if bypass-then-arm does not clear the keypad. It is unverified on a Concord 4 / BAT-Connect.

## pyalula

The integration vendors its own copy of the Alula client at `custom_components/alula/pyalula` (version 0.2.0). Home Assistant does not install `pyalula` from PyPI.

The same client still lives at the repository root for local use:

```python
from pyalula import AlulaClient

async with AlulaClient() as client:
    await client.login("user@example.com", "password")
    panel = await client.get_panel_status()
    print(panel.arm_state)
    await client.arm("away")
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
