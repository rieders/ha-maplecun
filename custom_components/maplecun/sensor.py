"""Sensoren: Wireless-M-Bus-Zähler, Revolt-Energiemesser und Diagnose."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
import json
import logging
from pathlib import Path
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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import MapleCunConfigEntry
from .const import (
    CONFLICT_WMBUS,
    CONF_METERS,
    CONF_MODEL,
    CONF_NAME,
    CONF_REVOLTS,
    CONF_SENSORS,
    DOMAIN,
    HEARD_LIST_MAX,
    MODULE_IDS,
    RAW_STATE_MAX,
    ROLE_RF433,
    WMBUS_ROLES,
    signal_climate,
    signal_connection,
    signal_meter,
    signal_meter_new,
    signal_meter_raw,
    signal_module_info,
    signal_raw,
    signal_rx,
    signal_revolt,
    signal_seen,
)

_LOGGER = logging.getLogger(__name__)

# Englische Namen aus strings.json als Rückfall: Home Assistant nimmt entity_description.name
# nur, wenn die Übersetzung fehlt (Entity._name_internal). So zeigt eine Entität nie nur den
# Gerätenamen, auch wenn die Übersetzungsdatei der Installation veraltet ist.
_FALLBACK_NAMES: dict[str, str] = {
    k: v.get("name", "")
    for k, v in json.loads((Path(__file__).parent / "strings.json").read_text(encoding="utf-8"))
    ["entity"]["sensor"].items()
}


def _with_fallback_name(entity: SensorEntity) -> None:
    """entity_description.name aus der Übersetzung (englisch) setzen, falls noch leer."""
    key = entity.translation_key
    if not key or key not in _FALLBACK_NAMES:
        return
    name = _FALLBACK_NAMES[key]
    placeholders = getattr(entity, "_attr_translation_placeholders", None) or {}
    try:
        name = name.format(**placeholders)
    except (KeyError, IndexError):
        pass
    desc = getattr(entity, "entity_description", None) or SensorEntityDescription(key=key)
    entity.entity_description = replace(desc, name=name)


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

# Speichernummern: 0 aktuell, 1 Stichtag, 8 (Wasser) bzw. 17 (HKV) Monatsende
_DUE = 1
_MONTH_END = {"volume": 8, "hca": 17}
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
    if key == "last_received":
        return MapleSensorDescription(
            key=key, translation_key="meter_last_received", device_class=SensorDeviceClass.TIMESTAMP,
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
    due = storage == _DUE
    month = storage != 0 and storage in _MONTH_END.values()
    if storage and not due and not month:
        return None                      # sonstige Speicher (Halbmonatswerte o. Ä.) weglassen
    if month and q not in ("date", *(k for k, s in _MONTH_END.items() if s == storage)):
        return None
    # Stichtags- und Monatsendwerte sind Diagnose-Sensoren ohne Statistik
    hist = due or month
    diag = EntityCategory.DIAGNOSTIC if hist else None
    suffix = "_due" if due else "_month" if month else ""

    if q == "volume":
        dc = SensorDeviceClass.GAS if type_name in _GAS_TYPES else (
            SensorDeviceClass.WATER if type_name in _WATER_TYPES else SensorDeviceClass.VOLUME)
        return MapleSensorDescription(
            key=key, translation_key=f"volume{suffix}",
            device_class=dc, native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            state_class=None if hist else SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=3, entity_category=diag,
        )
    if q == "energy" and not month:
        return MapleSensorDescription(
            key=key, translation_key="energy_due" if due else "meter_energy",
            device_class=SensorDeviceClass.ENERGY, native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=None if due else SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=1, entity_category=diag,
        )
    if q == "hca":
        # dimensionslos; total_increasing, da am Stichtag auf 0 zurückgesetzt
        return MapleSensorDescription(
            key=key, translation_key=f"hca{suffix}",
            state_class=None if hist else SensorStateClass.TOTAL_INCREASING,
            entity_category=diag,
        )
    if q == "date" and hist:
        return MapleSensorDescription(
            key=key, translation_key="due_date" if due else "month_date",
            device_class=SensorDeviceClass.DATE, entity_category=EntityCategory.DIAGNOSTIC,
        )
    if hist:
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

    # Diagnose-Entitäten abgeschalteter Module und den einzelnen Rohdaten-Sensor
    # aus Version 0.1 aus der Registry entfernen (sonst verwaist ohne Namen)
    ent_reg = er.async_get(hass)
    stale = {f"{entry.entry_id}_last_raw"} | {
        f"{entry.entry_id}_{kind}_{mid}"
        for mid in MODULE_IDS if mid not in hub.modules
        for kind in ("last_raw", "last_rx", "rx_today", "module", "mode")
    } | {f"{entry.entry_id}_mode_{mid}" for mid, mod in hub.modules.items() if not mod.is_wmbus}
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent.unique_id in stale:
            ent_reg.async_remove(ent.entity_id)

    wmbus = any(m.role in WMBUS_ROLES for m in hub.modules.values())
    rf_module = hub.module_for_role(ROLE_RF433)

    # Alle Sensor-Einträge entfernen, die diese Version nicht mehr anlegt: alte Schlüssel,
    # frühere unique_ids, Entitäten entfernter Module/Zähler. Solche verwaisten Einträge
    # zeigen in der Oberfläche nur den Gerätenamen (kein Name, kein Zustand).
    expected = _expected_sensor_ids(entry, hub, wmbus)
    # Ist gerade kein wMBus- bzw. 433-MHz-Modul eingerichtet, bleiben die Einträge dieser
    # Geräte stehen (Rolle evtl. nur vorübergehend geändert).
    keep = tuple(
        f"{entry.entry_id}_{p}_" for p, active in
        (("meter", wmbus), ("revolt", rf_module), ("climate", rf_module)) if not active
    )
    removed = []
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent.domain == "sensor" and ent.unique_id not in expected and not (keep and ent.unique_id.startswith(keep)):
            removed.append(f"{ent.entity_id} ({ent.unique_id})")
            ent_reg.async_remove(ent.entity_id)
    if removed:
        _LOGGER.info("Verwaiste Sensor-Einträge entfernt: %s", ", ".join(removed))

    # Rohdaten je Modul (Diagnose, standardmäßig aus)
    entities.extend(RawSensor(entry, mid) for mid in sorted(hub.modules))

    # --- 433 MHz / Revolt (eingerichtete Geräte)
    revolt_cfg: dict = entry.options.get(CONF_REVOLTS, {})
    if rf_module:
        for rid in sorted(hub.revolts):
            name = revolt_cfg.get(rid, {}).get(CONF_NAME) or f"Revolt {rid}"
            entities.extend(RevoltSensor(entry, rf_module.module_id, rid, name, d) for d in REVOLT_SENSORS)

    # --- Empfangsstatistik je Modul
    for mid in sorted(hub.modules):
        entities.append(LastRxSensor(entry, mid))
        entities.append(RxCountSensor(entry, mid))
        if hub.modules[mid].is_wmbus:
            entities.append(ModeSensor(entry, mid))

    # --- Temperatur-/Feuchtesensoren 433 MHz
    if rf_module:
        sensors_cfg: dict = entry.options.get(CONF_SENSORS, {})
        for key, cfg in sensors_cfg.items():
            for desc in climate_descriptions(cfg.get(CONF_MODEL, "")):
                entities.append(ClimateSensor(entry, rf_module.module_id, key, cfg, desc))

    # --- Wireless M-Bus
    if wmbus:
        entities.append(SeenMetersSensor(entry))
        meters_cfg: dict = entry.options.get(CONF_METERS, {})
        created: set[tuple[str, str]] = set()
        for meter_id in sorted(hub.meters):
            type_name = hub.seen.get(meter_id, {}).get("type", "")
            cfg = meters_cfg.get(meter_id, {})
            if meter_id not in hub.seen:
                continue          # noch nie empfangen: Entitäten entstehen mit dem ersten Telegramm
            # Schon empfangene Zähler: Rohzeile und "Zuletzt empfangen" immer anlegen (auch wenn
            # meter_keys aus einer älteren Version stammt); neue Schlüssel nicht doppelt anlegen
            created.add((meter_id, "last_telegram"))
            entities.append(MeterRawSensor(entry, meter_id, cfg, type_name))
            for key in ("last_received", *hub.meter_keys.get(meter_id, [])):
                if (meter_id, key) not in created and (desc := meter_description(key, type_name)):
                    created.add((meter_id, key))
                    entities.append(MeterSensor(entry, meter_id, cfg, type_name, desc, None))

        @callback
        def _new_meter_values(meter_id: str, tele, new_keys: list[str], values: dict) -> None:
            new = []
            cfg = meters_cfg.get(meter_id, {})
            if (meter_id, "last_telegram") not in created:
                created.add((meter_id, "last_telegram"))
                new.append(MeterRawSensor(entry, meter_id, cfg, tele.type_name))
            for key in new_keys:
                if (meter_id, key) in created:
                    continue
                created.add((meter_id, key))
                if desc := meter_description(key, tele.type_name):
                    new.append(MeterSensor(entry, meter_id, meters_cfg.get(meter_id, {}), tele.type_name, desc, values))
            if new:
                async_add_entities(new)

        entry.async_on_unload(async_dispatcher_connect(hass, signal_meter_new(entry.entry_id), _new_meter_values))

    async_add_entities(entities)


def _expected_sensor_ids(entry, hub, wmbus: bool) -> set[str]:
    """unique_ids aller Sensoren, die async_setup_entry (jetzt oder später) anlegt."""
    e = entry.entry_id
    ids = set()
    for mid, mod in hub.modules.items():
        ids |= {f"{e}_last_raw_{mid}", f"{e}_last_rx_{mid}", f"{e}_rx_today_{mid}"}
        if mod.is_wmbus:
            ids.add(f"{e}_mode_{mid}")
        ids |= {f"{e}_{k}_{mid}" for k in MODULE_INFO_KEYS}
    if hub.module_for_role(ROLE_RF433):
        ids |= {f"{e}_revolt_{rid}_{d.key}" for rid in hub.revolts for d in REVOLT_SENSORS}
        for key, cfg in entry.options.get(CONF_SENSORS, {}).items():
            ids |= {f"{e}_climate_{key}_{d.key}" for d in climate_descriptions(cfg.get(CONF_MODEL, ""))}
    if wmbus:
        ids.add(f"{e}_seen_meters")
        for mid in hub.meters:
            ids |= {f"{e}_meter_{mid}_last_telegram", f"{e}_meter_{mid}_last_received"}
            type_name = hub.seen.get(mid, {}).get("type", "")
            ids |= {f"{e}_meter_{mid}_{k}" for k in hub.meter_keys.get(mid, []) if meter_description(k, type_name)}
    return ids


# Diagnose-Sensoren je Modul aus Aufgabe 3 (hier nur für die Bereinigung bekannt)
MODULE_INFO_KEYS: tuple[str, ...] = ()


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


def meter_device(entry, hub, meter_id: str, cfg: dict, type_name: str) -> DeviceInfo:
    seen = hub.seen.get(meter_id, {})
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_meter_{meter_id}")},
        name=cfg.get(CONF_NAME) or f"{type_name or 'Zähler'} {meter_id}",
        manufacturer=seen.get("manufacturer"),
        model=type_name or None,
        serial_number=meter_id,
        via_device=(DOMAIN, entry.entry_id),
    )


class MeterSensor(_Base, RestoreSensor):
    """Messwert eines Wireless-M-Bus-Zählers."""

    entity_description: MapleSensorDescription

    def __init__(self, entry, meter_id: str, cfg: dict, type_name: str,
                 desc: MapleSensorDescription, initial: dict | None) -> None:
        super().__init__(entry, None)
        self.entity_description = desc
        self._meter_id = meter_id
        self._attr_unique_id = f"{entry.entry_id}_meter_{meter_id}_{desc.key}"
        self._attr_device_info = meter_device(entry, self._hub, meter_id, cfg, type_name)
        if initial and desc.key in initial:
            self._attr_native_value = _to_native(initial[desc.key], desc)
        _with_fallback_name(self)

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
        _with_fallback_name(self)

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
    """Diagnose: letzte Rohzeile eines Moduls.

    433 MHz / Überwachen: nicht ausgewertete Zeilen; Wireless M-Bus: jede Telegrammzeile
    (auch fremder Zähler – deshalb standardmäßig deaktiviert). Der Zustand ist auf
    255 Zeichen gekürzt, die vollständige Zeile steht im Attribut "line".
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _unrecorded_attributes = frozenset({"line"})

    def __init__(self, entry, module_id: str) -> None:
        super().__init__(entry, module_id)
        self._attr_unique_id = f"{entry.entry_id}_last_raw_{module_id}"
        # feste Übersetzung je Modul ("Rohdaten Modul N"), ohne Platzhalter
        self._attr_translation_key = f"last_raw_{module_id}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        self._attr_extra_state_attributes = {"line": None}
        _with_fallback_name(self)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_raw(self._entry.entry_id, self._mid), self._update)
        )

    @callback
    def _update(self, line: str) -> None:
        self._attr_native_value = line[:RAW_STATE_MAX]
        self._attr_extra_state_attributes = {"line": line}
        self.async_write_ha_state()


class SeenMetersSensor(_Base):
    """Diagnose: Zählerübersicht.

    Zustand: Anzahl der seit dem Start empfangenen unterschiedlichen Zähler.
    Attribute "eigene" (angelegte Zähler) und "weitere" (nicht angelegte, nur
    Kopfdaten, stärkstes Signal zuerst). Die Daten leben nur im Arbeitsspeicher
    und sind vom Recorder ausgenommen.
    """

    _attr_translation_key = "seen_meters"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"eigene", "weitere"})
    _WRITE_INTERVAL = 10.0

    def __init__(self, entry) -> None:
        super().__init__(entry, None)
        self._attr_unique_id = f"{entry.entry_id}_seen_meters"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        _with_fallback_name(self)
        self._last_write = 0.0
        self._unsub_later = None
        self._known = 0

    @property
    def native_value(self) -> int:
        return len(self._hub.heard)

    def _row(self, mid: str, info: dict, name: str) -> dict:
        return {
            "id": mid,
            "name": name,
            "hersteller": info.get("manufacturer"),
            "typ": info.get("type"),
            "rssi": info.get("rssi"),
            "modul": info.get("module"),
            "netzwerkknoten": bool(info.get("relayed")),
            "letzter_empfang": info.get("last_seen"),     # ISO 8601, Ortszeit
            "telegramme_heute": self._hub.heard_today(info),
        }

    @property
    def extra_state_attributes(self) -> dict:
        hub = self._hub
        cfg: dict = self._entry.options.get(CONF_METERS, {})
        own = []
        for mid in sorted(hub.meters):
            # seit dem Start noch nicht gehört: gespeicherte Kopfdaten des eigenen Zählers
            # (letzter Empfang bleibt so über Neustarts erhalten; leer = nie empfangen)
            info = hub.heard.get(mid) or hub.seen.get(mid, {})
            type_name = info.get("type") or ""
            name = cfg.get(mid, {}).get(CONF_NAME) or f"{type_name or 'Zähler'} {mid}"
            own.append(self._row(mid, info, name))
        others = [self._row(mid, info, "") for mid, info in hub.heard_unconfigured()[:HEARD_LIST_MAX]]
        return {"eigene": own, "weitere": others}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_seen(self._entry.entry_id), self._changed)
        )
        self.async_on_remove(self._cancel_later)

    @callback
    def _cancel_later(self) -> None:
        if self._unsub_later:
            self._unsub_later()
            self._unsub_later = None

    @callback
    def _changed(self) -> None:
        """Neue Zähler sofort, sonst höchstens alle 10 s schreiben (Attribute sind groß)."""
        now = time.monotonic()
        count = len(self._hub.heard)
        if count != self._known or now - self._last_write >= self._WRITE_INTERVAL:
            self._write(None)
        elif self._unsub_later is None:
            self._unsub_later = async_call_later(
                self.hass, self._WRITE_INTERVAL - (now - self._last_write), self._write
            )

    @callback
    def _write(self, _now) -> None:
        self._cancel_later()
        self._known = len(self._hub.heard)
        self._last_write = time.monotonic()
        self.async_write_ha_state()


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
        _with_fallback_name(self)

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
        _with_fallback_name(self)

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
        _with_fallback_name(self)

    @property
    def native_value(self):
        mod = self._hub.modules.get(self._mid)
        return mod.rx_count if mod else None


class ModeSensor(_Base):
    """Diagnose: letzte Antwort der Firmware auf den Empfangsmodus-Befehl eines wMBus-Moduls.

    Zustand: "SMODE"/"TMODE"/"CMODE"/"OFF", "nicht unterstützt", "belegt durch Modul N"
    oder leer (noch keine Antwort).
    """

    _attr_translation_key = "mode_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry, module_id: str) -> None:
        super().__init__(entry, module_id)
        self._attr_unique_id = f"{entry.entry_id}_mode_{module_id}"
        self._attr_translation_placeholders = {"module": module_id}
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        _with_fallback_name(self)

    @property
    def available(self) -> bool:
        return True                  # zeigt auch den Zustand eines getrennten Moduls

    @property
    def native_value(self) -> str | None:
        mod = self._hub.modules.get(self._mid)
        if mod is None:
            return None
        if mod.unsupported:
            return "nicht unterstützt"
        if mod.conflict:
            return f"belegt durch Modul {mod.conflict}"
        return mod.mode_reply[:RAW_STATE_MAX] if mod.mode_reply else None

    @property
    def extra_state_attributes(self) -> dict:
        mod = self._hub.modules.get(self._mid)
        if mod is None:
            return {}
        return {
            "rolle": mod.role,
            "befehl": mod.mode_cmd,
            "antwort": mod.mode_reply,
            "zeit": mod.mode_time.isoformat() if mod.mode_time else None,
            "grund": mod.unsupported or (CONFLICT_WMBUS if mod.conflict else None),
            "firmware": mod.version,
            "kommandos": mod.cmds,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_module_info(self._entry.entry_id), self.async_write_ha_state)
        )


class MeterRawSensor(_Base, RestoreEntity):
    """Diagnose: Rohzeile des letzten Telegramms eines angelegten Zählers.

    Zustand: Rohzeile (auf 255 Zeichen gekürzt), vollständig im Attribut "line" (nicht
    aufgezeichnet). Gefüllt über hub.deliver – Live-Empfang und FHEM-Import gleich.
    Nur für angelegte Zähler; Telegramme fremder Zähler erreichen diesen Sensor nie.
    """

    _attr_translation_key = "meter_last_telegram"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"line", "inneres_telegramm"})

    def __init__(self, entry, meter_id: str, cfg: dict, type_name: str) -> None:
        super().__init__(entry, None)
        self._meter_id = meter_id
        self._attr_unique_id = f"{entry.entry_id}_meter_{meter_id}_last_telegram"
        self._attr_device_info = meter_device(entry, self._hub, meter_id, cfg, type_name)
        self._attr_extra_state_attributes = {}
        if info := self._hub.meter_raw.get(meter_id):
            self._apply(info)
        _with_fallback_name(self)

    @property
    def available(self) -> bool:
        return self._attr_native_value is not None or super().available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._attr_native_value is None and (last := await self.async_get_last_state()):
            if last.state not in ("unknown", "unavailable"):
                self._attr_native_value = last.state
                self._attr_extra_state_attributes = dict(last.attributes)
                for k in ("friendly_name", "icon", "device_class"):
                    self._attr_extra_state_attributes.pop(k, None)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_meter_raw(self._entry.entry_id, self._meter_id), self._update
            )
        )

    @callback
    def _update(self, info: dict) -> None:
        self._apply(info)
        self.async_write_ha_state()

    def _apply(self, info: dict) -> None:
        line = info["line"]
        meter_time = info.get("meter_time")
        if isinstance(meter_time, datetime) and meter_time.tzinfo is None:
            meter_time = meter_time.replace(tzinfo=dt_util.get_default_time_zone())
        read_at = meter_time if isinstance(meter_time, datetime) else info["received"]
        self._attr_native_value = line[:RAW_STATE_MAX]
        self._attr_extra_state_attributes = {
            "line": line,
            "modul": info.get("module"),
            "netzwerkknoten": bool(info.get("relayed")),
            "ablesezeit": dt_util.as_local(read_at).isoformat(timespec="seconds"),
            "empfangen": dt_util.as_local(info["received"]).isoformat(timespec="seconds"),
            "lqi": info.get("lqi"),
            "rssi": info.get("rssi"),
            "inneres_telegramm": info.get("inner"),
        }
