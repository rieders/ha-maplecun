"""Sensoren: Wireless-M-Bus-Zähler, Revolt-Energiemesser und Diagnose."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
import time

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    PERCENTAGE,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import MapleCunConfigEntry
from .const import (
    CONF_METERS,
    CONF_MODEL,
    CONF_NAME,
    CONF_REVOLTS,
    CONF_SENSORS,
    DOMAIN,
    ROLE_RF433,
    WMBUS_ROLES,
    signal_climate,
    signal_connection,
    signal_meter,
    signal_meter_new,
    signal_raw,
    signal_rx,
    signal_revolt,
    signal_seen,
)

# ======================================================================= Revolt


@dataclass(frozen=True, kw_only=True)
class MapleSensorDescription(SensorEntityDescription):
    """Sensorbeschreibung."""


REVOLT_SENSORS: tuple[MapleSensorDescription, ...] = (
    MapleSensorDescription(
        key="power", translation_key="power",
        device_class=SensorDeviceClass.POWER, state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT, suggested_display_precision=1,
    ),
    MapleSensorDescription(
        key="energy", translation_key="energy",
        device_class=SensorDeviceClass.ENERGY, state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, suggested_display_precision=2,
    ),
    MapleSensorDescription(
        key="voltage", translation_key="voltage",
        device_class=SensorDeviceClass.VOLTAGE, state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT, suggested_display_precision=0,
    ),
    MapleSensorDescription(
        key="current", translation_key="current",
        device_class=SensorDeviceClass.CURRENT, state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE, suggested_display_precision=2,
    ),
    MapleSensorDescription(
        key="frequency", translation_key="frequency",
        device_class=SensorDeviceClass.FREQUENCY, state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ, suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
    MapleSensorDescription(
        key="pf", translation_key="power_factor",
        device_class=SensorDeviceClass.POWER_FACTOR, state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
)

# ======================================================================= Wireless M-Bus

_GAS_TYPES = {"Gas"}
_WATER_TYPES = {"Wasser", "Warmwasser", "Kaltwasser"}
_KEY_RE = re.compile(r"^(?P<q>[a-z_]+?)(?:_t(?P<t>\d+))?(?:_s(?P<s>\d+))?$")


def meter_description(key: str, type_name: str) -> MapleSensorDescription | None:
    """Passende Sensorbeschreibung zu einem Datensatz-Schlüssel (oder None = ignorieren)."""
    if key == "rssi":
        return MapleSensorDescription(
            key=key, translation_key="rssi",
            device_class=SensorDeviceClass.SIGNAL_STRENGTH, state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
    if key == "datetime":
        return MapleSensorDescription(
            key=key, translation_key="meter_time", device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False,
        )
    m = _KEY_RE.match(key)
    if not m or m.group("t"):
        return None
    q, storage = m.group("q"), int(m.group("s") or 0)
    if storage not in (0, 1):
        return None                      # Monatswerte etc. weglassen
    due = storage == 1

    if q == "volume":
        dc = SensorDeviceClass.GAS if type_name in _GAS_TYPES else (
            SensorDeviceClass.WATER if type_name in _WATER_TYPES else SensorDeviceClass.VOLUME)
        return MapleSensorDescription(
            key=key, translation_key="volume_due" if due else "volume",
            device_class=dc, native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            state_class=None if due else SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=3,
        )
    if q == "energy":
        return MapleSensorDescription(
            key=key, translation_key="energy_due" if due else "meter_energy",
            device_class=SensorDeviceClass.ENERGY, native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=None if due else SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=1,
        )
    if q == "hca":
        return MapleSensorDescription(
            key=key, translation_key="hca_due" if due else "hca",
            state_class=None if due else SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement="Einheiten",
        )
    if q == "date" and due:
        return MapleSensorDescription(key=key, translation_key="due_date", device_class=SensorDeviceClass.DATE)
    if due:
        return None
    if q == "power":
        return MapleSensorDescription(
            key=key, translation_key="power", device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfPower.WATT,
        )
    if q == "volume_flow":
        return MapleSensorDescription(
            key=key, translation_key="volume_flow", device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
            entity_registry_enabled_default=False,
        )
    if q in ("flow_temperature", "return_temperature", "external_temperature"):
        return MapleSensorDescription(
            key=key, translation_key=q, device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            suggested_display_precision=1,
        )
    return None


# ======================================================================= Setup


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MapleCunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities: list[SensorEntity] = []

    # Geräte entfernter Zähler aufräumen
    dev_reg = dr.async_get(hass)
    wanted = (
        {f"{entry.entry_id}_meter_{mid}" for mid in hub.meters}
        | {f"{entry.entry_id}_revolt_{rid}" for rid in hub.revolts}
        | {f"{entry.entry_id}_climate_{key}" for key in hub.climate}
    )
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        for domain, ident in device.identifiers:
            if (
                domain == DOMAIN
                and ident.startswith(
                    (f"{entry.entry_id}_meter_", f"{entry.entry_id}_revolt_", f"{entry.entry_id}_climate_")
                )
                and ident not in wanted
            ):
                dev_reg.async_remove_device(device.id)

    # Rohdaten je Modul (Diagnose, standardmäßig aus)
    entities.extend(RawSensor(entry, mid) for mid in sorted(hub.modules))

    # --- 433 MHz / Revolt (eingerichtete Geräte)
    rf_module = hub.module_for_role(ROLE_RF433)
    revolt_cfg: dict = entry.options.get(CONF_REVOLTS, {})
    if rf_module:
        for rid in sorted(hub.revolts):
            name = revolt_cfg.get(rid, {}).get(CONF_NAME) or f"Revolt {rid}"
            entities.extend(RevoltSensor(entry, rf_module.module_id, rid, name, d) for d in REVOLT_SENSORS)

    # --- Empfangsstatistik je Modul
    for mid in sorted(hub.modules):
        entities.append(LastRxSensor(entry, mid))
        entities.append(RxCountSensor(entry, mid))

    # --- Temperatur-/Feuchtesensoren 433 MHz
    if rf_module:
        sensors_cfg: dict = entry.options.get(CONF_SENSORS, {})
        for key, cfg in sensors_cfg.items():
            for desc in climate_descriptions(cfg.get(CONF_MODEL, "")):
                entities.append(ClimateSensor(entry, rf_module.module_id, key, cfg, desc))

    # --- Wireless M-Bus
    if any(m.role in WMBUS_ROLES for m in hub.modules.values()):
        entities.append(SeenMetersSensor(entry))
        meters_cfg: dict = entry.options.get(CONF_METERS, {})
        for meter_id, keys in hub.meter_keys.items():
            type_name = hub.seen.get(meter_id, {}).get("type", "")
            for key in keys:
                if desc := meter_description(key, type_name):
                    entities.append(MeterSensor(entry, meter_id, meters_cfg.get(meter_id, {}), type_name, desc, None))

        @callback
        def _new_meter_values(meter_id: str, tele, new_keys: list[str], values: dict) -> None:
            new = []
            for key in new_keys:
                if desc := meter_description(key, tele.type_name):
                    new.append(MeterSensor(entry, meter_id, meters_cfg.get(meter_id, {}), tele.type_name, desc, values))
            if new:
                async_add_entities(new)

        entry.async_on_unload(async_dispatcher_connect(hass, signal_meter_new(entry.entry_id), _new_meter_values))

    async_add_entities(entities)


# ======================================================================= Entitäten


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: MapleCunConfigEntry, module_id: str | None) -> None:
        self._entry = entry
        self._hub = entry.runtime_data
        self._mid = module_id

    @property
    def available(self) -> bool:
        if self._mid is None:
            return any(m.connected for m in self._hub.modules.values())
        return self._hub.is_connected(self._mid)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_connection(self._entry.entry_id), self.async_write_ha_state)
        )


def _to_native(value, desc: SensorEntityDescription):
    """Werte für HA aufbereiten (Datum/Zeitstempel)."""
    if desc.device_class == SensorDeviceClass.TIMESTAMP and isinstance(value, datetime):
        return value.replace(tzinfo=dt_util.get_default_time_zone()) if value.tzinfo is None else value
    if desc.device_class == SensorDeviceClass.DATE:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
    return value


class MeterSensor(_Base, RestoreSensor):
    """Messwert eines Wireless-M-Bus-Zählers."""

    entity_description: MapleSensorDescription

    def __init__(self, entry, meter_id: str, cfg: dict, type_name: str,
                 desc: MapleSensorDescription, initial: dict | None) -> None:
        super().__init__(entry, None)
        self.entity_description = desc
        self._meter_id = meter_id
        self._attr_unique_id = f"{entry.entry_id}_meter_{meter_id}_{desc.key}"
        seen = self._hub.seen.get(meter_id, {})
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_meter_{meter_id}")},
            name=cfg.get(CONF_NAME) or f"{type_name or 'Zähler'} {meter_id}",
            manufacturer=seen.get("manufacturer"),
            model=type_name or None,
            serial_number=meter_id,
            via_device=(DOMAIN, entry.entry_id),
        )
        if initial and desc.key in initial:
            self._attr_native_value = _to_native(initial[desc.key], desc)

    @property
    def available(self) -> bool:
        # Zähler senden selten – nach einem Verbindungsabbruch den letzten Wert behalten
        return self._attr_native_value is not None or super().available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._attr_native_value is None and (last := await self.async_get_last_sensor_data()):
            self._attr_native_value = _to_native(last.native_value, self.entity_description)
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_meter(self._entry.entry_id, self._meter_id), self._update)
        )

    @callback
    def _update(self, values: dict) -> None:
        if self.entity_description.key in values:
            self._attr_native_value = _to_native(values[self.entity_description.key], self.entity_description)
            self.async_write_ha_state()


class RevoltSensor(_Base, RestoreSensor):
    """Messwert eines Revolt NC-5462."""

    entity_description: MapleSensorDescription

    def __init__(self, entry, module_id: str, rid: str, name: str, desc: MapleSensorDescription) -> None:
        super().__init__(entry, module_id)
        self.entity_description = desc
        self._rid = rid
        self._attr_unique_id = f"{entry.entry_id}_revolt_{rid}_{desc.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_revolt_{rid}")},
            name=name, manufacturer="Revolt", model="NC-5462", serial_number=rid,
            via_device=(DOMAIN, entry.entry_id),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._attr_native_value is None and (last := await self.async_get_last_sensor_data()):
            self._attr_native_value = last.native_value
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_revolt(self._entry.entry_id, self._rid), self._update)
        )

    @callback
    def _update(self, values: dict) -> None:
        if (value := values.get(self.entity_description.key)) is not None:
            self._attr_native_value = value
            self.async_write_ha_state()


class RawSensor(_Base):
    """Diagnose: letzte nicht ausgewertete Zeile eines Moduls."""

    _attr_translation_key = "last_raw"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, entry, module_id: str) -> None:
        super().__init__(entry, module_id)
        self._attr_unique_id = f"{entry.entry_id}_last_raw_{module_id}"
        self._attr_translation_placeholders = {"module": module_id}
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_raw(self._entry.entry_id, self._mid), self._update)
        )

    @callback
    def _update(self, line: str) -> None:
        self._attr_native_value = line[:255]
        self.async_write_ha_state()


class SeenMetersSensor(_Base):
    """Diagnose: Anzahl empfangener Wireless-M-Bus-Zähler (Liste in den Attributen)."""

    _attr_translation_key = "seen_meters"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"meters"})

    def __init__(self, entry) -> None:
        super().__init__(entry, None)
        self._attr_unique_id = f"{entry.entry_id}_seen_meters"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def native_value(self) -> int:
        return len(self._hub.seen)

    @property
    def extra_state_attributes(self) -> dict:
        configured = set(self._hub.meters)
        rows = sorted(self._hub.seen.items(), key=lambda kv: kv[1].get("last_seen", ""), reverse=True)
        return {
            "meters": [
                {"id": mid, **info, "configured": mid in configured} for mid, info in rows[:100]
            ]
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_seen(self._entry.entry_id), self.async_write_ha_state)
        )


# ======================================================================= Thermometer 433 MHz

_HUMIDITY_MODELS = {"GT_WT_02", "Type1", "KW9010"}


def climate_descriptions(model: str) -> list[MapleSensorDescription]:
    descs = [
        MapleSensorDescription(
            key="temperature", translation_key="temperature", device_class=SensorDeviceClass.TEMPERATURE,
            state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            suggested_display_precision=1,
        ),
        MapleSensorDescription(
            key="rssi", translation_key="rssi", device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
            entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False,
        ),
    ]
    if model in _HUMIDITY_MODELS:
        descs.insert(1, MapleSensorDescription(
            key="humidity", translation_key="humidity", device_class=SensorDeviceClass.HUMIDITY,
            state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=PERCENTAGE,
        ))
    return descs


def climate_device(entry, key: str, cfg: dict) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_climate_{key}")},
        name=cfg.get(CONF_NAME) or f"Thermometer {key}",
        manufacturer="433 MHz",
        model=cfg.get(CONF_MODEL) or None,
        serial_number=key,
        via_device=(DOMAIN, entry.entry_id),
    )


class ClimateSensor(_Base, RestoreSensor):
    """Temperatur/Feuchte eines 433-MHz-Thermometers.

    Die Sensoren senden jede Messung mehrfach; Zustände werden nur bei Änderung
    oder spätestens alle 5 Minuten geschrieben.
    """

    entity_description: MapleSensorDescription

    def __init__(self, entry, module_id: str, key: str, cfg: dict, desc: MapleSensorDescription) -> None:
        super().__init__(entry, module_id)
        self.entity_description = desc
        self._key = key
        self._last_write = 0.0
        self._attr_unique_id = f"{entry.entry_id}_climate_{key}_{desc.key}"
        self._attr_device_info = climate_device(entry, key, cfg)

    @property
    def available(self) -> bool:
        return self._attr_native_value is not None or super().available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_climate(self._entry.entry_id, self._key), self._update)
        )

    @callback
    def _update(self, values: dict) -> None:
        value = values.get(self.entity_description.key)
        if value is None:
            return
        now = time.monotonic()
        if value == self._attr_native_value and now - self._last_write < 300:
            return
        self._attr_native_value = value
        self._last_write = now
        self.async_write_ha_state()


# ======================================================================= Empfangsstatistik


class LastRxSensor(_Base):
    """Zeitpunkt des letzten empfangenen Telegramms eines Moduls."""

    _attr_translation_key = "last_rx"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry, module_id: str) -> None:
        super().__init__(entry, module_id)
        self._attr_unique_id = f"{entry.entry_id}_last_rx_{module_id}"
        self._attr_translation_placeholders = {"module": module_id}
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def native_value(self):
        mod = self._hub.modules.get(self._mid)
        return mod.last_rx if mod else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_rx(self._entry.entry_id), self.async_write_ha_state)
        )


class RxCountSensor(LastRxSensor):
    """Anzahl empfangener Zeilen seit Mitternacht."""

    _attr_translation_key = "rx_today"
    _attr_device_class = None
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, entry, module_id: str) -> None:
        super().__init__(entry, module_id)
        self._attr_unique_id = f"{entry.entry_id}_rx_today_{module_id}"

    @property
    def native_value(self):
        mod = self._hub.modules.get(self._mid)
        return mod.rx_count if mod else None
