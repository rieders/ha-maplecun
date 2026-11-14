"""Rohdaten-Sensoren, Zählerübersicht und letzter Empfang."""

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maplecun.const import DOMAIN
from custom_components.maplecun.sensor import RawSensor, SeenMetersSensor

from .test_maplecun import RELAYED_HCA, REPEATED, WARM, _modules, _settle, firmware, mods  # noqa: F401


def _entry(mods, roles=("wmbus_c", "rf433", "off", "monitor"), meters=None):
    return MockConfigEntry(
        domain=DOMAIN, version=2, title="MapleCUN test", unique_id="127.0.0.1",
        data={"host": "127.0.0.1", "modules": _modules(mods, roles)},
        options={"init_mode": True, "revolts": {}, "it_devices": {}, "meters": meters or {}},
    )


# ------------------------------------------------------------------ Rohdaten
async def test_raw_sensors_named_and_long_lines(hass: HomeAssistant, mods):
    entry = _entry(mods, ("wmbus_c", "rf433", "monitor", "wmbus_t"))
    entry.add_to_hass(hass)
    ents = er.async_get(hass)
    # verwaiste Einträge: Rohdaten-Sensor aus 0.1 (ohne Modulnummer)
    ents.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_last_raw", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    regs = {e.unique_id: e for e in er.async_entries_for_config_entry(ents, entry.entry_id)}
    assert f"{entry.entry_id}_last_raw" not in regs
    for n in "1234":
        e = regs[f"{entry.entry_id}_last_raw_{n}"]
        assert e.original_name == f"Raw data module {n}" and e.translation_key == f"last_raw_{n}"
        assert e.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert e.entity_category == "diagnostic"

    for n in "12":
        ents.async_update_entity(f"sensor.maplecun_test_raw_data_module_{n}", disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await _settle(hass, 0.6)

    # wMBus: jede Telegrammzeile, auch > 255 Zeichen (hier mit CRC-Fehler)
    line = WARM + "FF" * 100
    await mods["1"].send(line)
    await _settle(hass)
    st = hass.states.get("sensor.maplecun_test_raw_data_module_1")
    assert len(line) > 255 and st.state == line[:255] and st.attributes["line"] == line
    await mods["1"].send(WARM)
    await _settle(hass)
    assert hass.states.get("sensor.maplecun_test_raw_data_module_1").state == WARM
    # 433 MHz: weiterhin nur nicht ausgewertete Zeilen
    await mods["2"].send("unbekannte Zeile")
    await _settle(hass)
    assert hass.states.get("sensor.maplecun_test_raw_data_module_2").state == "unbekannte Zeile"
    await hass.config_entries.async_unload(entry.entry_id)

    # Modul abgeschaltet -> dessen Diagnose-Entitäten werden entfernt
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "modules": _modules(mods, ("wmbus_c", "rf433", "off", "off"))})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    uids = {e.unique_id for e in er.async_entries_for_config_entry(ents, entry.entry_id)}
    assert f"{entry.entry_id}_last_raw_3" not in uids and f"{entry.entry_id}_module_4" not in uids
    assert f"{entry.entry_id}_last_raw_2" in uids
    await hass.config_entries.async_unload(entry.entry_id)


def test_unrecorded_attributes():
    assert "line" in RawSensor._unrecorded_attributes
    assert {"eigene", "weitere"} <= SeenMetersSensor._unrecorded_attributes


# ------------------------------------------------------------------ Zählerübersicht
async def test_meter_overview(hass: HomeAssistant, mods, hass_storage):
    entry = _entry(mods, meters={"52144701": {"name": "Warmwasser Bad", "key": ""},
                                 "11111111": {"name": "", "key": ""}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    st = hass.states.get("sensor.maplecun_test_received_meters")
    assert st.state == "0"
    own = {r["id"]: r for r in st.attributes["eigene"]}
    assert set(own) == {"52144701", "11111111"}
    assert own["11111111"]["letzter_empfang"] is None and own["11111111"]["telegramme_heute"] == 0
    assert st.attributes["weitere"] == []

    await mods["1"].send(WARM)
    await mods["1"].send(WARM)
    await mods["1"].send(REPEATED)                          # fremd, über Netzwerkknoten, -70.5 dBm
    await mods["1"].send(RELAYED_HCA + "C8")                # fremd, LQI 80 + RSSI C8 = -102 dBm
    await _settle(hass, 0.5)
    hub = entry.runtime_data
    st = hass.states.get("sensor.maplecun_test_received_meters")
    assert st.state == "3"
    own = {r["id"]: r for r in st.attributes["eigene"]}
    w = own["52144701"]
    assert (w["name"], w["hersteller"], w["typ"], w["rssi"], w["modul"], w["netzwerkknoten"]) == (
        "Warmwasser Bad", "LSE", "Warmwasser", -70.5, "1", False)
    assert w["letzter_empfang"] and w["telegramme_heute"] == 2
    assert own["11111111"]["name"] == "Zähler 11111111"
    assert own["11111111"]["letzter_empfang"] is None
    others = st.attributes["weitere"]
    assert [r["id"] for r in others] == ["52143906", "16660259"]        # stärkstes Signal zuerst
    assert others[0]["netzwerkknoten"] is True and others[0]["typ"] == "Wasser"
    assert others[0]["telegramme_heute"] == 1 and others[0]["rssi"] == -70.5
    assert others[1]["rssi"] == -102.0 and others[1]["typ"] == "Heizkostenverteiler"

    # fremde Zähler: keine Messwerte, keine Entitäten, nichts gespeichert
    assert set(hub.meter_keys) == {"52144701"} and set(hub.seen) == {"52144701"}
    for info in hub.heard.values():
        assert set(info) <= {"manufacturer", "type", "device_type", "rssi", "module", "relayed",
                             "encrypted", "last_seen", "count_today", "day"}
    assert hass.states.get("sensor.wasser_52143906_meter_reading") is None

    # Options-Flow: Vorschläge = gehörte, nicht angelegte IDs, stärkste zuerst
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "add_meter"})
    schema = r["data_schema"].schema
    key = next(k for k in schema if str(k) == "meter_id")
    cfg = schema[key].config
    assert [o["value"] for o in cfg["options"]] == ["52143906", "16660259"]
    assert cfg["options"][0]["label"] == "52143906 – Wasser, -70.5 dBm"
    assert cfg["custom_value"] is True
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"meter_id": "16660259"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await _settle(hass, 1.0)
    # nach dem Neuladen (wie nach einem Neustart): Liste beginnt leer
    hub = entry.runtime_data
    assert hub.heard == {}
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = str(hass_storage.get(f"{DOMAIN}.{entry.entry_id}", {}))
    assert "52143906" not in stored and "52143906" not in str(dict(entry.options))


def test_header_only_decodes_no_values():
    from custom_components.maplecun import wmbus

    frame, _ = wmbus.parse_cul_line(REPEATED)
    head = wmbus.decode(frame, header_only=True)
    assert (head.meter_id, head.type_name, head.relayed) == ("52143906", "Wasser", True)
    assert head.records == []


# ------------------------------------------------------------------ Letzter Empfang
async def test_last_received_restored(hass: HomeAssistant, mods):
    entry = _entry(mods, meters={"52144701": {"name": "", "key": ""}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    await mods["1"].send(WARM)
    await _settle(hass)
    eid = "sensor.warmwasser_52144701_last_received"
    st = hass.states.get(eid)
    assert st.attributes["device_class"] == "timestamp"
    first = st.state
    assert first not in ("unknown", "unavailable")
    # Neuladen ohne neues Telegramm: Wert bleibt erhalten
    await hass.config_entries.async_reload(entry.entry_id)
    await _settle(hass, 0.6)
    assert hass.states.get(eid).state == first
    own = hass.states.get("sensor.maplecun_test_received_meters").attributes["eigene"]
    assert own[0]["letzter_empfang"] is not None and own[0]["telegramme_heute"] == 0
    await hass.config_entries.async_unload(entry.entry_id)
