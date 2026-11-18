# Changelog

## 0.2.1

Live check on a Concord 4 with BAT-Connect: `alula.force_arm` with `method: bypass` leaves the panel disarmed and the keypad on the indirect-bypass prompt, and the zones are not bypassed. `method: native` (`forceArm`, `userNumber` 0) arms cleanly: the keypad goes to the arming countdown, the panel becomes armed, and open zones show `bypassed: true`. Home Assistant state can lag the keypad by 30-90 seconds.

- `alula.force_arm` defaults to `method: native`. `bypass` is still accepted and is documented as not working on a Concord 4.
- Arm Home, Arm Away, Arm Night, and Arm custom bypass use native `forceArm`. Arming still does not use or store a PIN.
- Entity unique ids are unchanged.

## 0.2.0

- Vendored `pyalula` inside the integration and stopped depending on the PyPI release.
- Added `alula.force_arm` and `alula.bypass_zone`, plus `ARM_CUSTOM_BYPASS` on the alarm panel.
