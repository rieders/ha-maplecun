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
CONF_SENSORS = "climate_sensors"
CONF_MODEL = "model"
CONF_AUTO = "auto_add"
CONF_LOG_UNKNOWN = "log_unknown_meters"
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

# Empfangsmodus je Rolle (a-culfw clib/rf_mbus.c, rf_mbus_func):
#   "brs" -> rf_mbus_init(S-Modus), Antwort "SMODE"
#   "brt" -> rf_mbus_init(T-Modus), Antwort "TMODE"
#   "brc" -> culfw (svn.code.sf.net/p/culfw): C-Modus, Antwort "CMODE".
#            a-culfw 1.26.08 kennt keinen C-Modus: "brc" fällt in den Aus-Zweig, Antwort "OFF".
#   "brx" (jedes andere Zeichen) -> aus, Antwort "OFF"; "br"/"b" allein -> nur Status.
# Die Befehle schalten nicht um (kein Toggle): sie initialisieren den CC1101 jedes Mal neu
# und sind damit idempotent. "X21" (set_txreport) schaltet das Modul auf SlowRF – es muss
# deshalb VOR dem wMBus-Befehl kommen (wie in FHEM 00_CUL.pm: initString "X21\nbrs").
ROLE_MODE_CMD = {ROLE_WMBUS_C: "brc", ROLE_WMBUS_T: "brt", ROLE_WMBUS_S: "brs"}
MODE_REPLY = {"brc": "CMODE", "brt": "TMODE", "brs": "SMODE"}
MODE_REPLY_RE = r"^(SMODE|TMODE|CMODE|OFF)$|is unknown"
ROLE_INIT = {
    ROLE_WMBUS_C: ("X21", "brc"),
    ROLE_WMBUS_T: ("X21", "brt"),
    ROLE_WMBUS_S: ("X21", "brs"),
    ROLE_RF433: ("X21",),
}
# Benötigte Firmware-Kommandos je Rolle (Antwort auf "?")
ROLE_NEEDS = {ROLE_WMBUS_C: "b", ROLE_WMBUS_T: "b", ROLE_WMBUS_S: "b", ROLE_RF433: "Xi"}

# Gründe, warum eine Rolle auf einem Modul nicht funktioniert
UNSUPPORTED_NO_B = "no_b"             # Kommandoliste ohne "b"
UNSUPPORTED_UNKNOWN = "unknown"       # Antwort "? (brc is unknown) …"
UNSUPPORTED_NO_CMODE = "no_cmode"     # "brc" -> "OFF": Firmware ohne C-Modus (a-culfw)
CONFLICT_WMBUS = "wmbus_exclusive"    # a-culfw: nur ein Modul gleichzeitig im wMBus-Modus

# Vorbelegung passend zu maplecun-splitproxy
DEFAULT_MODULES = {
    "1": {"port": 1080, CONF_ROLE: ROLE_WMBUS_T},   # a-culfw (MapleCUN) kennt keinen C-Modus
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
KIND_SENSOR = "sensor"
DEFAULT_AUTO = {KIND_REVOLT: True, KIND_IT: False, KIND_METER: False, KIND_SENSOR: False}

RECONNECT_DELAY = 5
MODE_RETRY_DELAY = 300      # unaufgefordertes "OFF" eines wMBus-Moduls: höchstens alle 5 min neu setzen
REPLY_TIMEOUT = 3.0         # Wartezeit auf eine Firmware-Antwort (s)
WATCHDOG_SILENCE = 45 * 60  # wMBus-Modul ohne jede Zeile -> Modus neu setzen …
WATCHDOG_REPEAT = 60 * 60   # … höchstens einmal pro Stunde
WATCHDOG_CHECK = 5 * 60     # Prüfintervall
STORAGE_VERSION = 1
SEEN_MAX = 200
HEARD_MAX = 1000          # Zählerübersicht (nur Arbeitsspeicher)
HEARD_LIST_MAX = 200      # davon im Attribut "weitere"
RAW_STATE_MAX = 255       # HA-Grenze für Zustände


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


def signal_meter_raw(entry_id: str, meter_id: str) -> str:
    """Rohzeile des letzten Telegramms eines angelegten Zählers."""
    return f"{DOMAIN}_{entry_id}_meter_raw_{meter_id}"


def signal_seen(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_seen"


def signal_it(entry_id: str, code: str) -> str:
    return f"{DOMAIN}_{entry_id}_it_{code}"


def signal_discovered(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_discovered"


def signal_climate(entry_id: str, key: str) -> str:
    return f"{DOMAIN}_{entry_id}_climate_{key}"


def signal_rx(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_rx"


def signal_module_info(entry_id: str) -> str:
    """Firmware-Antworten (Version, Kommandos, Empfangsmodus, Modulwerte) geändert."""
    return f"{DOMAIN}_{entry_id}_module_info"
