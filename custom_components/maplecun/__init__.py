"""MapleCUN (a-culfw) – alle Funkmodule direkt in Home Assistant.

  * Wireless M-Bus (C/T/S): Wasser-, Wärmezähler, Heizkostenverteiler
  * 433 MHz: Revolt NC-5462 Energiemesser, Intertechno-Steckdosen
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_IT_DEVICES,
    CONF_METERS,
    CONF_MODULES,
    CONF_NAME,
    CONF_REVOLTS,
    CONF_ROLE,
    DEFAULT_MODULES,
    DOMAIN,
    ROLE_OFF,
    ROLE_RF433,
)
from .hub import MapleCunHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]

type MapleCunConfigEntry = ConfigEntry[MapleCunHub]


async def async_setup_entry(hass: HomeAssistant, entry: MapleCunConfigEntry) -> bool:
    hub = MapleCunHub(hass, entry)
    entry.runtime_data = hub

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="a-culfw",
        model="MapleCUN",
        configuration_url="https://wiki.fhem.de/wiki/MapleCUN",
    )

    await hub.async_start()

    # Revolts aus Version 0.1/0.2 (Speicher) in die Optionen übernehmen
    if CONF_REVOLTS not in entry.options:
        revolts = {rid: {CONF_NAME: f"Revolt {rid}"} for rid in hub.legacy_revolts}
        hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_REVOLTS: revolts})
        hub.revolts = set(revolts)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: MapleCunConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MapleCunConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Version 1 (nur 433-MHz-Port) -> Version 2 (vier Module)."""
    if entry.version == 1:
        modules = {mid: dict(cfg) for mid, cfg in DEFAULT_MODULES.items()}
        for cfg in modules.values():
            cfg[CONF_ROLE] = ROLE_OFF
        old_port = entry.data.get("port", 1081)
        slot = next((m for m, c in DEFAULT_MODULES.items() if c["port"] == old_port), "2")
        modules[slot] = {"port": old_port, CONF_ROLE: ROLE_RF433}
        hass.config_entries.async_update_entry(
            entry,
            data={"host": entry.data["host"], CONF_MODULES: modules},
            options={**entry.options, CONF_METERS: entry.options.get(CONF_METERS, {})},
            unique_id=entry.data["host"],
            version=2,
        )
        _LOGGER.info("MapleCUN-Eintrag auf Version 2 migriert")
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: MapleCunConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Zähler, Revolts und Steckdosen dürfen über die Geräteseite gelöscht werden."""
    prefixes = {
        f"{entry.entry_id}_meter_": CONF_METERS,
        f"{entry.entry_id}_revolt_": CONF_REVOLTS,
        f"{entry.entry_id}_it_": CONF_IT_DEVICES,
    }
    for domain, ident in device.identifiers:
        if domain != DOMAIN:
            continue
        for prefix, opt in prefixes.items():
            if ident.startswith(prefix):
                dev_id = ident[len(prefix):]
                items = dict(entry.options.get(opt, {}))
                items.pop(dev_id, None)
                if opt == CONF_METERS:
                    await entry.runtime_data.async_forget_meter(dev_id)
                hass.config_entries.async_update_entry(entry, options={**entry.options, opt: items})
                return True
    return False
