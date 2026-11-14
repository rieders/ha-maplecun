"""Verknüpfung der Teilgeräte (Zähler, Revolt, Thermometer, Intertechno) mit dem MapleCUN."""

from __future__ import annotations

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
