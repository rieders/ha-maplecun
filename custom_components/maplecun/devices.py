"""Verknüpfung der Teilgeräte (Zähler, Revolt, Thermometer, Intertechno) mit dem MapleCUN."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

# Home Assistant 2026.x: DeviceInfo(via_device=(domain, id)) ist veraltet und wird ab 2027.8
# abgelehnt; neu ist via_device_id (ID aus der Geräteregistrierung). Ältere Versionen kennen
# nur via_device.
_HAS_VIA_DEVICE_ID = any(
    "via_device_id" in getattr(cls, "__annotations__", {}) for cls in getattr(DeviceInfo, "__mro__", (DeviceInfo,))
)


def via_hub(entry) -> dict:
    """Schlüsselwort für DeviceInfo, das auf das MapleCUN-Gerät dieses Eintrags zeigt."""
    if not _HAS_VIA_DEVICE_ID:
        return {"via_device": (DOMAIN, entry.entry_id)}
    device_id = getattr(entry.runtime_data, "device_id", None)
    return {"via_device_id": device_id} if device_id else {}


def it_device(entry, code: str, dev: dict) -> DeviceInfo:
    """Gerät einer Intertechno-Steckdose (Schalter, Signalstärke, letzter Tastendruck)."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_it_{code}")},
        name=dev.get("name") or f"Intertechno {code}",
        manufacturer="Intertechno",
        model=f"Tristate {code}",
        **via_hub(entry),
    )


def remote_device(entry, house: str) -> DeviceInfo:
    """Gerät einer Fernbedienung (Hauscode): Batterie-Frühwarnung."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_remote_{house}")},
        name=f"Fernbedienung {house}",
        manufacturer="Intertechno",
        model=f"Hauscode {house}",
        **via_hub(entry),
    )


_LOGGER = logging.getLogger(__name__)


def remove_orphans(hass: HomeAssistant, entry, domain: str, expected: set[str]) -> None:
    """Registry-Einträge einer Plattform entfernen, die diese Version nicht (mehr) anlegt.

    Sonst bleiben z. B. Einstellungen eines Moduls mit Rolle "Aus" oder entfallene Entitäten
    als "nicht verfügbar" ohne Namen (nur Gerätename) auf der Geräteseite stehen.
    """
    reg = er.async_get(hass)
    removed = []
    for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
        if ent.domain == domain and ent.unique_id not in expected:
            removed.append(f"{ent.entity_id} ({ent.unique_id})")
            reg.async_remove(ent.entity_id)
    if removed:
        _LOGGER.info("Verwaiste %s-Einträge entfernt: %s", domain, ", ".join(removed))
