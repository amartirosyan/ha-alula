"""Constants for the Alula Home Assistant integration."""

from .pyalula import const as _pyalula_const

# Re-exported so the service and Arm Night share the client's variant list.
FORCE_ARM_VARIANTS = _pyalula_const.ARM_VARIANTS
DEFAULT_NIGHT_ARM_VARIANT = _pyalula_const.DEFAULT_ARM_VARIANT

DOMAIN = "alula"

CONF_PANEL_ID = "panel_id"

DEFAULT_SCAN_INTERVAL = 30  # seconds

# Entry data keys
DATA_CLIENT = "client"
DATA_COORDINATOR = "coordinator"

SERVICE_FORCE_ARM = "force_arm"
SERVICE_BYPASS_ZONE = "bypass_zone"

ATTR_MODE = "mode"
ATTR_METHOD = "method"
ATTR_ZONE = "zone"
ATTR_BYPASS = "bypass"
ATTR_SILENT = "silent"
ATTR_NO_ENTRY_DELAY = "no_entry_delay"
ATTR_VARIANT = "variant"

# Options-flow key. Arm Night reads this; missing means DEFAULT_NIGHT_ARM_VARIANT.
CONF_NIGHT_ARM_VARIANT = "night_arm_variant"

FORCE_ARM_MODES = ("home", "away", "night")
FORCE_ARM_METHODS = ("bypass", "native")
DEFAULT_FORCE_ARM_METHOD = "native"
