"""MapleCUN (a-culfw) – alle Funkmodule direkt in Home Assistant.

  * Wireless M-Bus (C/T/S): Wasser-, Wärmezähler, Heizkostenverteiler
  * 433 MHz: Revolt NC-5462 Energiemesser, Intertechno-Steckdosen
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import (
    CONF_IT_DEVICES,
    CONF_METERS,
    CONF_MODULES,
    CONF_NAME,
    CONF_REVOLTS,
    CONF_ROLE,
    CONF_SENSORS,
    DEFAULT_MODULES,
    DOMAIN,
    ROLE_OFF,
    ROLE_RF433,
)
from .hub import MapleCunHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH,
]

type MapleCunConfigEntry = ConfigEntry[MapleCunHub]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_IMPORT_FHEM_LOG = "import_fhem_log"
SERVICE_SEND_RAW = "send_raw"
SEND_RAW_SCHEMA = vol.Schema(
    {
        vol.Required("module"): vol.In(["1", "2", "3", "4"]),
        vol.Required("command"): vol.All(cv.string, vol.Length(min=1, max=200)),
        vol.Optional("force", default=False): cv.boolean,
        vol.Optional("timeout", default=3.0): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=10)),
        vol.Optional("config_entry_id"): cv.string,
    }
)
IMPORT_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Optional("dry_run", default=True): cv.boolean,
        vol.Optional("overwrite", default=False): cv.boolean,
        vol.Optional("config_entry_id"): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Aktionen registrieren."""

    def _entry_for(call: ServiceCall):
        entries = [
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED
            and (not call.data.get("config_entry_id") or e.entry_id == call.data["config_entry_id"])
        ]
        if len(entries) != 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_entry" if not entries else "multiple_entries",
            )
        return entries

    async def _send_raw(call: ServiceCall) -> ServiceResponse:
        """FHEM "set/get raw": Kommando an ein Modul, erste Antwortzeile als response data."""
        hub = _entry_for(call)[0].runtime_data
        module, command = call.data["module"], call.data["command"].strip()
        if module not in hub.modules:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="module_not_active",
                                         translation_placeholders={"module": module})
        try:
            reply = await hub.async_send_raw(module, command, force=call.data["force"], timeout=call.data["timeout"])
        except PermissionError as err:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="raw_blocked",
                                         translation_placeholders={"command": command}) from err
        except (ConnectionError, OSError) as err:
            raise HomeAssistantError(str(err)) from err
        return {"module": module, "command": command, "response": reply}

    hass.services.async_register(
        DOMAIN, SERVICE_SEND_RAW, _send_raw, schema=SEND_RAW_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )

    async def _import_fhem_log(call: ServiceCall) -> ServiceResponse:
        from . import importer

        entries = _entry_for(call)
        path = call.data["path"]
        allowed = await hass.async_add_executor_job(hass.config.is_allowed_path, path)
        if not allowed:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="path_not_allowed", translation_placeholders={"path": path}
            )
        try:
            analysis = await importer.async_analyze_file(hass, entries[0].runtime_data, path)
        except OSError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="file_error",
                translation_placeholders={"path": path, "error": str(err)},
            ) from err
        return await importer.async_import(
            hass, entries[0], analysis, dry_run=call.data["dry_run"], overwrite=call.data["overwrite"]
        )

    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_FHEM_LOG, _import_fhem_log,
        schema=IMPORT_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
    return True


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
        f"{entry.entry_id}_climate_": CONF_SENSORS,
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
