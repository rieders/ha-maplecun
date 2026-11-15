"""Einstellungen als Auswahl: Bandbreite, Verstärkung, Empfindlichkeit.

Werte und Stufen wie FHEM 00_CUL.pm ("set bWidth/rAmpl/sens"), von a-culfw im EEPROM
gespeichert. Nur im 433-MHz-Betrieb, standardmäßig deaktiviert, nie automatisch geschrieben.
Angezeigt wird der aus dem Modul gelesene Wert (ccconf). PA-Tabelle und LED gibt es nicht mehr
als Entität: a-culfw meldet beide nicht zurück (Auswahl blieb leer bzw. "nicht verfügbar");
setzen weiterhin über die Aktion maplecun.send_raw ("x…", "l…").
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MapleCunConfigEntry, culcmd
from .const import DOMAIN, ROLE_RF433, signal_connection, signal_module_info
from .devices import remove_orphans
from .naming import with_fallback_name


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MapleCunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[SelectEntity] = []
    for mid, mod in sorted(hub.modules.items()):
        if mod.role != ROLE_RF433 or (mod.cmds and "W" not in mod.cmds):
            continue              # wMBus: eigene Funkeinstellungen; ohne "W" nie verfügbar
        entities += [RadioSelect(entry, mid, k) for k in ("bwidth", "rampl", "sens")]
    remove_orphans(hass, entry, "select", {e.unique_id for e in entities})
    async_add_entities(entities)


class _Base(SelectEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: MapleCunConfigEntry, module_id: str, key: str) -> None:
        self._entry = entry
        self._hub = entry.runtime_data
        self._mid = module_id
        self._attr_unique_id = f"{entry.entry_id}_set_{key}_{module_id}"
        self._attr_translation_key = f"set_{key}"
        self._attr_translation_placeholders = {"module": module_id}
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        with_fallback_name(self, "select", SelectEntityDescription)

    @property
    def available(self) -> bool:
        return self._hub.is_connected(self._mid) and self._hub.can_configure_radio(self._mid)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for sig in (signal_connection, signal_module_info):
            self.async_on_remove(async_dispatcher_connect(self.hass, sig(self._entry.entry_id), self.async_write_ha_state))


_OPTIONS = {
    "bwidth": [str(b) for b in culcmd.bandwidths()],
    "rampl": [str(a) for a in culcmd.AMPL_LIST],
    "sens": [str(s) for s in culcmd.SENS_LIST],
}


class RadioSelect(_Base):
    """bWidth (kHz), rAmpl (dB), sens (dB); Zustand = gelesener Wert aus ccconf."""

    def __init__(self, entry, module_id: str, key: str) -> None:
        super().__init__(entry, module_id, key)
        self._key = key
        self._attr_options = _OPTIONS[key]

    @property
    def current_option(self) -> str | None:
        value = self._hub.modules[self._mid].info.get(self._key)
        return str(int(value)) if value is not None and str(int(value)) in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        try:
            await self._hub.async_write_radio(self._mid, self._key, int(option))
        except (ValueError, ConnectionError, OSError) as err:
            raise HomeAssistantError(str(err)) from err
