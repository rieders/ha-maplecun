"""MapleCUN (a-culfw) – 433-MHz-Geräte direkt in Home Assistant.

Unterstützt:
  * Revolt NC-5462 Energiemesser  (werden automatisch erkannt)
  * Intertechno-Steckdosen (Tristate)  (über die Optionen anlegen)
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .hub import MapleCunHub

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]

type MapleCunConfigEntry = ConfigEntry[MapleCunHub]


async def async_setup_entry(hass: HomeAssistant, entry: MapleCunConfigEntry) -> bool:
    """Integration einrichten."""
    hub = MapleCunHub(hass, entry.entry_id, entry.data[CONF_HOST], entry.data[CONF_PORT])
    entry.runtime_data = hub

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="a-culfw",
        model="MapleCUN 433 MHz",
        configuration_url="https://wiki.fhem.de/wiki/MapleCUN",
    )

    await hub.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: MapleCunConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MapleCunConfigEntry) -> bool:
    """Integration entladen."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: MapleCunConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Löschen eines Revolt-Geräts über die Oberfläche erlauben."""
    for domain, ident in device.identifiers:
        if domain == DOMAIN and ident.startswith(f"{entry.entry_id}_revolt_"):
            await entry.runtime_data.async_forget_revolt(ident.rsplit("_", 1)[-1])
            return True
    # Hub und Intertechno-Geräte werden über die Optionen verwaltet
    return False
