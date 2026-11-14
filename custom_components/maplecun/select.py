"""Einstellungen als Auswahl: Bandbreite, Verstärkung, Empfindlichkeit, PA-Tabelle, LED.

Werte und Stufen wie FHEM 00_CUL.pm ("set bWidth/rAmpl/sens/patable/led"). bWidth, rAmpl,
sens, patable und led werden von a-culfw im EEPROM gespeichert. Nur im 433-MHz-Betrieb
(LED: überall, wo die Firmware "l" kennt), standardmäßig deaktiviert, nie automatisch.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import MapleCunConfigEntry, culcmd
from .const import DOMAIN, ROLE_RF433, signal_connection, signal_module_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MapleCunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[SelectEntity] = []
    for mid, mod in sorted(hub.modules.items()):
        if mod.role == ROLE_RF433:
            entities += [RadioSelect(entry, mid, k) for k in ("bwidth", "rampl", "sens")]
            if mid == "1":
                entities.append(PatableSelect(entry, mid))
        if (mod.cmds and "l" in mod.cmds) or (not mod.cmds and mid == "1"):
            entities.append(LedSelect(entry, mid))
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


class _Remembered(_Base, RestoreEntity):
    """Die Firmware meldet den Wert nicht zurück: zuletzt gewählte Option anzeigen (nicht senden)."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in self._attr_options:
            self._attr_current_option = last.state


class PatableSelect(_Remembered):
    """Sendeleistung (PA-Tabelle 00–09). a-culfw schreibt sie nur in den Bereich von Modul 1."""

    def __init__(self, entry, module_id: str) -> None:
        super().__init__(entry, module_id, "patable")
        self._attr_options = list(culcmd.PATABLE)
        self._attr_current_option = None

    @property
    def available(self) -> bool:
        return super().available and "x" in (self._hub.modules[self._mid].cmds or "")

    async def async_select_option(self, option: str) -> None:
        try:
            await self._hub.async_set_patable(self._mid, option)
        except (ValueError, ConnectionError, OSError) as err:
            raise HomeAssistantError(str(err)) from err
        self._attr_current_option = option
        self.async_write_ha_state()


class LedSelect(_Remembered):
    """LED aus / an / blinken ("l00"/"l01"/"l02")."""

    def __init__(self, entry, module_id: str) -> None:
        super().__init__(entry, module_id, "led")
        self._attr_options = list(culcmd.LED_MODES)
        self._attr_current_option = None

    @property
    def available(self) -> bool:
        return self._hub.is_connected(self._mid)

    async def async_select_option(self, option: str) -> None:
        try:
            await self._hub.async_set_led(self._mid, option)
        except (ValueError, ConnectionError, OSError) as err:
            raise HomeAssistantError(str(err)) from err
        self._attr_current_option = option
        self.async_write_ha_state()
