"""Konstanten für die MapleCUN-Integration."""

DOMAIN = "maplecun"

# --- Konfiguration
CONF_MODULES = "modules"
CONF_ROLE = "role"
CONF_INIT = "init_mode"
CONF_IT_DEVICES = "it_devices"
CONF_IT_CODES = "it_codes"
CONF_METERS = "meters"
CONF_REVOLTS = "revolts"
CONF_AUTO = "auto_add"
CONF_METER_ID = "meter_id"
CONF_KEY = "key"
CONF_NAME = "name"
CONF_CODE = "code"
CONF_ON = "on"
CONF_OFF = "off"

MODULE_IDS = ("1", "2", "3", "4")

ROLE_OFF = "off"
ROLE_RF433 = "rf433"
ROLE_WMBUS_C = "wmbus_c"
ROLE_WMBUS_T = "wmbus_t"
ROLE_WMBUS_S = "wmbus_s"
ROLE_MONITOR = "monitor"
ROLES = (ROLE_WMBUS_C, ROLE_WMBUS_T, ROLE_WMBUS_S, ROLE_RF433, ROLE_MONITOR, ROLE_OFF)
WMBUS_ROLES = (ROLE_WMBUS_C, ROLE_WMBUS_T, ROLE_WMBUS_S)

# a-culfw-Befehle, um ein Modul in den passenden Empfangsmodus zu setzen
ROLE_INIT = {
    ROLE_WMBUS_C: ("X21", "brc"),
    ROLE_WMBUS_T: ("X21", "brt"),
    ROLE_WMBUS_S: ("X21", "brs"),
    ROLE_RF433: ("X21",),
}

# Vorbelegung passend zu maplecun-splitproxy
DEFAULT_MODULES = {
    "1": {"port": 1080, CONF_ROLE: ROLE_WMBUS_C},
    "2": {"port": 1081, CONF_ROLE: ROLE_RF433},
    "3": {"port": 1082, CONF_ROLE: ROLE_OFF},
    "4": {"port": 1083, CONF_ROLE: ROLE_OFF},
}

DEFAULT_IT_ON = "FF"
DEFAULT_IT_OFF = "F0"

# Automatisch übernehmen je Gerätetyp (Vorgabe)
KIND_METER = "meter"
KIND_IT = "it"
KIND_REVOLT = "revolt"
DEFAULT_AUTO = {KIND_REVOLT: True, KIND_IT: False, KIND_METER: False}

RECONNECT_DELAY = 5
STORAGE_VERSION = 1
SEEN_MAX = 200


def signal_connection(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_connection"


def signal_raw(entry_id: str, module: str) -> str:
    return f"{DOMAIN}_{entry_id}_raw_{module}"


def signal_revolt_new(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_revolt_new"


def signal_revolt(entry_id: str, rid: str) -> str:
    return f"{DOMAIN}_{entry_id}_revolt_{rid}"


def signal_meter_new(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_meter_new"


def signal_meter(entry_id: str, meter_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_meter_{meter_id}"


def signal_seen(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_seen"


def signal_it(entry_id: str, code: str) -> str:
    return f"{DOMAIN}_{entry_id}_it_{code}"


def signal_discovered(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_discovered"
