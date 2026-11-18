"""Constants for the Alula Home Assistant integration."""

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

FORCE_ARM_MODES = ("home", "away", "night")
FORCE_ARM_METHODS = ("bypass", "native")
DEFAULT_FORCE_ARM_METHOD = "native"
