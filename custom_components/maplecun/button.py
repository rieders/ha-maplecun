"""Buttons: Modulwerte aktualisieren, Modul neu verbinden (FHEM "set reopen")."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MapleCunConfigEntry
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MapleCunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[ButtonEntity] = [RefreshButton(entry)]
    entities.extend(ReopenButton(entry, mid) for mid in sorted(hub.modules))
    async_add_entities(entities)


class _Base(ButtonEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: MapleCunConfigEntry) -> None:
        self._entry = entry
        self._hub = entry.runtime_data
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})


class RefreshButton(_Base):
    """Alle Module abfragen (nur lesende Befehle; credit10ms/Laufzeit höchstens alle 15 min)."""

    _attr_translation_key = "refresh_info"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_refresh_info"

    async def async_press(self) -> None:
        await self._hub.async_refresh_all()


class ReopenButton(_Base):
    """Verbindung zum Modul neu aufbauen und das Modul neu initialisieren."""

    _attr_translation_key = "reopen"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry, module_id: str) -> None:
        super().__init__(entry)
        self._mid = module_id
        self._attr_unique_id = f"{entry.entry_id}_reopen_{module_id}"
        self._attr_translation_placeholders = {"module": module_id}

    async def async_press(self) -> None:
        await self._hub.async_reopen(self._mid)
