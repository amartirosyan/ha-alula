"""Constants for the Alula API — filled in from Phase 1 traffic capture."""

# -------------------------------------------------------------------
# Base URLs
# -------------------------------------------------------------------
BASE_URL = "https://api.alula.net"
WS_URL = "wss://api.alulaprod.com/ws/v1"

# -------------------------------------------------------------------
# OAuth2 — static client credentials shipped in the Alula mobile app.
# These are the same for all users and are not per-account secrets.
# -------------------------------------------------------------------
OAUTH_CLIENT_ID = "4ce837c4-08e2-11e7-aa3b-605718912297"
OAUTH_CLIENT_SECRET = "Uzka3sgLNDTaH3cQ"

# -------------------------------------------------------------------
# REST endpoint paths
# -------------------------------------------------------------------
PATH_TOKEN = "/oauth/token"
PATH_DEVICES_PANELS = "/rest/v1/devices?customOptions[omitRelationships]=true&filter[isPanel]=true&sort=friendlyName"
PATH_HELIX_USERS = "/rest/v1/helix/users"  # ?filter[deviceId]=<id>

# -------------------------------------------------------------------
# Arming level byte codes (from ArmingLevel enum + Firebase config)
# level1=disarm, level2=stay, level3=night, level4=away
# -------------------------------------------------------------------
ARM_LEVEL_DISARM = 1
ARM_LEVEL_STAY = 2
ARM_LEVEL_NIGHT = 3
ARM_LEVEL_AWAY = 4

# No-PIN payload shapes exposed so a panel can be live-tested.
# forcearm: armSilent and noEntryDelay inside the forceArm value (unverified;
# no captured command shows those keys on forceArm).
# arminglevel: the existing writeMfd armingLevel value, which already has them.
# partition: partitionArmingLevelChange, authType user, no PIN. That shape is
# proven on ConnectFlex and unverified on a Concord 4.
# forcearm_then_arminglevel: the proven forceArm write, a short pause, then
# armingLevel with the flags.
# arminglevel_then_*: answer a Concord 4 arming protest from Home Assistant.
# armingLevel with the flags starts the arm; with open zones the panel
# protests and the keypad shows its BYPASS / CANCEL prompt. After a short
# pause the second step answers that prompt: forceArm (forcearm), the same
# armingLevel write again (rearm, like a second keyfob Lock press), or
# zoneBypass for each open zone (zonebypass). All unverified on a Concord 4.
ARM_VARIANTS = (
    "forcearm",
    "arminglevel",
    "partition",
    "forcearm_then_arminglevel",
    "arminglevel_then_forcearm",
    "arminglevel_then_rearm",
    "arminglevel_then_zonebypass",
)
PROTEST_ANSWER_VARIANTS = {
    "arminglevel_then_forcearm": "forcearm",
    "arminglevel_then_rearm": "rearm",
    "arminglevel_then_zonebypass": "zonebypass",
}
# Arm Night uses this until a Concord 4 test shows which payload lands.
DEFAULT_ARM_VARIANT = "forcearm"

# String values returned by the REST API in the armingLevel field
ARM_STATE_STRINGS = {
    "disarm": ARM_LEVEL_DISARM,
    "stay": ARM_LEVEL_STAY,
    "night": ARM_LEVEL_NIGHT,
    "away": ARM_LEVEL_AWAY,
}

# -------------------------------------------------------------------
# WebSocket — device.helix channel
# -------------------------------------------------------------------
WS_CHANNEL = "device.helix"
WS_ACTION_SEND = "send"

# Zones per batch request (matches web app behaviour; firmware drops large ranges)
ZONE_BATCH_SIZE = 3

# -------------------------------------------------------------------
# Timeouts / retry
# -------------------------------------------------------------------
REQUEST_TIMEOUT = 15       # seconds, REST calls
WS_CONNECT_TIMEOUT = 10    # seconds, WebSocket handshake
WS_RESPONSE_TIMEOUT = 5    # seconds, wait for panel push after zone request
TOKEN_REFRESH_BUFFER = 300  # refresh token this many seconds before expiry
