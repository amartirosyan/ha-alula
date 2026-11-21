# Changelog

## 0.2.3

Supervised live test on a Concord 4 with BAT-Connect, Nov 21 2026, two windows open. With the keypad on its BYPASS / CANCEL prompt, `forceArm` was ACKed but did not arm, and a repeated `armingLevel` write did not arm either. `zoneBypass` for each open zone, sent while the prompt was up, armed Stay (silent exit, no entry delay) 8 to 15 s later with nobody touching the keypad (7:48:37 AM and 9:42:24 AM PT). `zoneBypass` sent while disarmed, before the arm request, does not take. The keypad chirps briefly at the prompt; that can't be avoided.

- New variant `arminglevel_then_zonebypass` (for `night_arm_variant` and `alula.force_arm`). It refreshes zone state, writes `armingLevel` with the flags, waits for the panel ACK/NAK (at least 3 s, at most 6 s), then sends `zoneBypass` for every zone open at that moment. Recommended Night setting.
- Fire and life-safety zones are never auto-bypassed: Alula smoke/CO types, raw zone types naming smoke, CO, gas, heat, water/flood/moisture, fire, panic or 24-hour, and any zone whose Home Assistant device class is smoke, carbon_monoxide, gas, moisture, heat or safety.
- `arminglevel_then_forcearm` and `arminglevel_then_rearm` are kept for diagnosis; neither armed a Concord 4.
- `zoneBypass` writes and disarm (`changeArmingLevelUsingCode`) now log the command and the panel ACK or NAK at INFO. The PIN is masked.
- Disarm without a code, or a failed disarm command, now raises an error in Home Assistant instead of failing silently.
- No PIN is sent or stored for arming.
- Faster state in Home Assistant: after any arm, disarm, force arm or zone bypass, the integration refreshes at about 3, 8, 15 and 25 s instead of waiting for the 30 s poll, and stops as soon as the arm state changes. A new command restarts the burst; refreshes never overlap (at most four extra calls per command).

## 0.2.2

Concord 4 has no Night arming level. Level 3 is ignored. Arm Night now sends Stay (arming level 2) with `armSilent` and `noEntryDelay`, and does not send or store a PIN. Home Assistant reports that as `armed_home`. The keypad text for success is `ARMED TO STAY NO DELAY`, with no exit beeps.

`armSilent` and `noEntryDelay` are unverified on a Concord 4. They are part of the `armingLevel` write this integration already sends, and of `partitionArmingLevelChange` with `authType: user` and no PIN (that command is proven on ConnectFlex). No captured command shows them on `forceArm`. The Arm Night button sends the `forcearm` variant until a live test shows which payload the panel accepts. Change it under Settings → Devices & services → Alula → Configure (`night_arm_variant`).

- `alula.force_arm` accepts `silent` and `no_entry_delay` (both default false) and `variant`: `forcearm`, `arminglevel`, `partition`, `forcearm_then_arminglevel`. Leaving `variant` out keeps the 0.2.1 command.
- Arm commands and the panel ack or NAK that answers them are logged at INFO. PINs are not logged.
- Arm Home, Arm Away, and Arm custom bypass are unchanged.

## 0.2.1

Live check on a Concord 4 with BAT-Connect: `alula.force_arm` with `method: bypass` leaves the panel disarmed and the keypad on the indirect-bypass prompt, and the zones are not bypassed. `method: native` (`forceArm`, `userNumber` 0) arms cleanly: the keypad goes to the arming countdown, the panel becomes armed, and open zones show `bypassed: true`. Home Assistant state can lag the keypad by 30-90 seconds.

- `alula.force_arm` defaults to `method: native`. `bypass` is still accepted and is documented as not working on a Concord 4.
- Arm Home, Arm Away, Arm Night, and Arm custom bypass use native `forceArm`. Arming still does not use or store a PIN.
- Entity unique ids are unchanged.

## 0.2.0

- Vendored `pyalula` inside the integration and stopped depending on the PyPI release.
- Added `alula.force_arm` and `alula.bypass_zone`, plus `ARM_CUSTOM_BYPASS` on the alarm panel.
