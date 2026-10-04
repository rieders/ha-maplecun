"""Sensoren: Revolt-Energiemesser und Rohdaten-Diagnose."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MapleCunConfigEntry
from .const import DOMAIN, signal_connection, signal_raw, signal_revolt, signal_revolt_new


@dataclass(frozen=True, kw_only=True)
class RevoltSensorDescription(SensorEntityDescription):
    """Beschreibung eines Revolt-Messwerts."""


REVOLT_SENSORS: tuple[RevoltSensorDescription, ...] = (
    RevoltSensorDescription(
        key="power", translation_key="power",
        device_class=SensorDeviceClass.POWER, state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT, suggested_display_precision=1,
    ),
    RevoltSensorDescription(
        key="energy", translation_key="energy",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2,
    ),
    RevoltSensorDescription(
        key="voltage", translation_key="voltage",
        device_class=SensorDeviceClass.VOLTAGE, state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT, suggested_display_precision=0,
    ),
    RevoltSensorDescription(
        key="current", translation_key="current",
        device_class=SensorDeviceClass.CURRENT, state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, suggested_display_precision=2,
    ),
    RevoltSensorDescription(
        key="frequency", translation_key="frequency",
        device_class=SensorDeviceClass.FREQUENCY, state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ, suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
    RevoltSensorDescription(
        key="pf", translation_key="power_factor",
        device_class=SensorDeviceClass.POWER_FACTOR, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MapleCunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Sensoren anlegen – bekannte Revolts sofort, neue sobald sie funken."""
    hub = entry.runtime_data

    def revolt_entities(rid: str, initial: dict | None = None) -> list[SensorEntity]:
        return [RevoltSensor(entry, rid, desc, initial) for desc in REVOLT_SENSORS]

    entities: list[SensorEntity] = [RawSensor(entry)]
    for rid in sorted(hub.known_revolts):
        entities.extend(revolt_entities(rid))
    async_add_entities(entities)

    @callback
    def _new_revolt(rid: str, values: dict) -> None:
        async_add_entities(revolt_entities(rid, values))

    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_revolt_new(entry.entry_id), _new_revolt)
    )


class _MapleCunEntity(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: MapleCunConfigEntry) -> None:
        self._entry = entry
        self._hub = entry.runtime_data

    @property
    def available(self) -> bool:
        return self._hub.connected

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_connection(self._entry.entry_id), self.async_write_ha_state
            )
        )


class RevoltSensor(_MapleCunEntity, RestoreSensor):
    """Ein Messwert eines Revolt NC-5462."""

    entity_description: RevoltSensorDescription

    def __init__(
        self,
        entry: MapleCunConfigEntry,
        rid: str,
        description: RevoltSensorDescription,
        initial: dict | None,
    ) -> None:
        super().__init__(entry)
        self.entity_description = description
        self._rid = rid
        self._attr_unique_id = f"{entry.entry_id}_revolt_{rid}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_revolt_{rid}")},
            name=f"Revolt {rid}",
            manufacturer="Revolt",
            model="NC-5462",
            via_device=(DOMAIN, entry.entry_id),
        )
        if initial:
            self._attr_native_value = initial.get(description.key)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._attr_native_value is None and (last := await self.async_get_last_sensor_data()):
            self._attr_native_value = last.native_value
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_revolt(self._entry.entry_id, self._rid), self._update
            )
        )

    @callback
    def _update(self, values: dict) -> None:
        if (value := values.get(self.entity_description.key)) is not None:
            self._attr_native_value = value
            self.async_write_ha_state()


class RawSensor(_MapleCunEntity):
    """Diagnose: letzte unbekannte Funkzeile (z.B. fremde Wettersensoren)."""

    _attr_translation_key = "last_raw"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry: MapleCunConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_last_raw"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_raw(self._entry.entry_id), self._update)
        )

    @callback
    def _update(self, line: str) -> None:
        self._attr_native_value = line[:255]
        self.async_write_ha_state()
