"""Verbindungsstatus je Funkmodul."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MapleCunConfigEntry
from .const import CONF_SENSORS, DOMAIN, ROLE_RF433, signal_climate, signal_connection, signal_it_press
from homeassistant.helpers import device_registry as dr

from .devices import remote_device, remove_orphans
from .naming import with_fallback_name


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MapleCunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[BinarySensorEntity] = [ModuleConnection(entry, mid) for mid in sorted(hub.modules)]
    rf = hub.module_for_role(ROLE_RF433)
    if rf:
        from .sensor import climate_device

        for key, cfg in entry.options.get(CONF_SENSORS, {}).items():
            entities.append(ClimateBattery(entry, key, cfg, climate_device(entry, key, cfg)))
        # Batterie-Frühwarnung je Fernbedienung (Hauscode der eingerichteten Intertechno-Geräte)
        entities.extend(RemoteBattery(entry, house) for house in sorted(hub.remote_houses()))
    expected = {e.unique_id for e in entities}
    if not rf:              # Einträge behalten, solange kein 433-MHz-Modul eingerichtet ist
        expected |= {f"{entry.entry_id}_climate_{key}_battery" for key in entry.options.get(CONF_SENSORS, {})}
        expected |= {f"{entry.entry_id}_remote_{h}_battery" for h in hub.remote_houses()}
    # Geräte von Fernbedienungen, deren Hauscode kein eingerichtetes Gerät mehr hat
    dev_reg = dr.async_get(hass)
    wanted = {f"{entry.entry_id}_remote_{h}" for h in hub.remote_houses()}
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if any(d == DOMAIN and i.startswith(f"{entry.entry_id}_remote_") and i not in wanted
               for d, i in device.identifiers):
            dev_reg.async_remove_device(device.id)
    remove_orphans(hass, entry, "binary_sensor", expected)
    async_add_entities(entities)


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
        with_fallback_name(self, "binary_sensor", BinarySensorEntityDescription)

    @property
    def is_on(self) -> bool:
        return self._hub.is_connected(self._mid)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_connection(self._entry.entry_id), self.async_write_ha_state
            )
        )


class ClimateBattery(BinarySensorEntity):
    """Batterie schwach (433-MHz-Thermometer)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry, key: str, cfg: dict, device_info: DeviceInfo) -> None:
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_climate_{key}_battery"
        self._attr_device_info = device_info
        self._attr_is_on = None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_climate(self._entry.entry_id, self._key), self._update)
        )

    @callback
    def _update(self, values: dict) -> None:
        low = values.get("battery_low")
        if low is not None and low != self._attr_is_on:
            self._attr_is_on = low
            self.async_write_ha_state()


class RemoteBattery(BinarySensorEntity):
    """Batterie der Fernbedienung schwach – aus dem Verlauf der Signalstärke (remote.py).

    Ein, wenn der Median der letzten 20 Tastendrücke mindestens <Schwelle> dB unter dem besten
    bisher erreichten Median liegt; unbekannt, solange weniger als 10 Tastendrücke vorliegen.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "remote_battery"

    def __init__(self, entry, house: str) -> None:
        self._entry = entry
        self._hub = entry.runtime_data
        self._house = house
        self._attr_unique_id = f"{entry.entry_id}_remote_{house}_battery"
        self._attr_device_info = remote_device(entry, house)
        with_fallback_name(self, "binary_sensor", BinarySensorEntityDescription)

    @property
    def is_on(self) -> bool | None:
        stats = self._hub.remotes.get(self._house)
        return stats.weak(self._hub.it_battery_db) if stats else None

    @property
    def extra_state_attributes(self) -> dict:
        stats = self._hub.remotes.get(self._house)
        return {
            "hauscode": self._house,
            "median_dbm": stats.median if stats else None,
            "referenz_dbm": stats.reference if stats else None,
            "tastendruecke": stats.presses if stats else 0,
            "verstuemmelt_anteil": stats.garbled_ratio if stats else None,
            "schwelle_db": self._hub.it_battery_db,
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_it_press(self._entry.entry_id), self.async_write_ha_state)
        )
