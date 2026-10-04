"""Verbindungsstatus je Funkmodul."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MapleCunConfigEntry
from .const import DOMAIN, signal_connection


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MapleCunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    async_add_entities(ModuleConnection(entry, mid) for mid in sorted(hub.modules))


class ModuleConnection(BinarySensorEntity):
    """Ist das Funkmodul (Port am Split-Proxy) verbunden?"""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "module"

    def __init__(self, entry: MapleCunConfigEntry, module_id: str) -> None:
        self._entry = entry
        self._hub = entry.runtime_data
        self._mid = module_id
        mod = self._hub.modules[module_id]
        self._attr_unique_id = f"{entry.entry_id}_module_{module_id}"
        self._attr_translation_placeholders = {"module": module_id}
        self._attr_extra_state_attributes = {"port": mod.port, "role": mod.role}
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def is_on(self) -> bool:
        return self._hub.is_connected(self._mid)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_connection(self._entry.entry_id), self.async_write_ha_state
            )
        )
