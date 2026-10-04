"""Konstanten für die MapleCUN-Integration."""

DOMAIN = "maplecun"

CONF_IT_DEVICES = "it_devices"
CONF_IT_CODES = "it_codes"
CONF_NAME = "name"
CONF_CODE = "code"
CONF_ON = "on"
CONF_OFF = "off"

DEFAULT_PORT = 1081
DEFAULT_IT_ON = "FF"
DEFAULT_IT_OFF = "F0"

RECONNECT_DELAY = 5
STORAGE_VERSION = 1


def signal_revolt_new(entry_id: str) -> str:
    """Signal: neuer Revolt-Sender entdeckt."""
    return f"{DOMAIN}_{entry_id}_revolt_new"


def signal_revolt(entry_id: str, rid: str) -> str:
    """Signal: neue Messwerte eines Revolt-Senders."""
    return f"{DOMAIN}_{entry_id}_revolt_{rid}"


def signal_raw(entry_id: str) -> str:
    """Signal: sonstige Funkzeile empfangen."""
    return f"{DOMAIN}_{entry_id}_raw"


def signal_connection(entry_id: str) -> str:
    """Signal: Verbindungsstatus geändert."""
    return f"{DOMAIN}_{entry_id}_connection"
