"""Diagnose-Sensor "Letztes Telegramm (roh)" je angelegtem Zähler."""

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from custom_components.maplecun.const import DOMAIN

from .test_maplecun import RELAYED_HCA, REPEATED, WARM, _modules, _settle, firmware, mods  # noqa: F401


def _entry(hass, mods, meters=("52144701", "16660259")):
    entry = MockConfigEntry(domain=DOMAIN, version=2, title="MapleCUN test", unique_id="127.0.0.1",
        data={"host": "127.0.0.1", "modules": _modules(mods)},
        options={"init_mode": True, "revolts": {}, "it_devices": {},
                 "meters": {m: {"name": "", "key": ""} for m in meters}})
    entry.add_to_hass(hass)
    return entry


async def test_raw_sensor_live(hass: HomeAssistant, mods):
    entry = _entry(hass, mods)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    await mods["1"].send(WARM)
    await mods["1"].send(RELAYED_HCA + "C8")           # über Netzwerkknoten, + RSSI-Byte
    await mods["1"].send(REPEATED)                     # fremder Zähler
    await _settle(hass, 0.6)

    st = hass.states.get("sensor.warmwasser_52144701_last_telegram_raw")
    assert st.state == WARM
    a = st.attributes
    assert a["line"] == WARM and a["modul"] == "1" and a["netzwerkknoten"] is False
    assert a["lqi"] == 0x80 and a["rssi"] == -70.5 and a["inneres_telegramm"] is None
    assert a["ablesezeit"].startswith("2026-10-04T08:13")      # Zeit aus dem Telegramm
    ent = er.async_get(hass).async_get(st.entity_id)
    assert ent.entity_category == "diagnostic"

    hca = hass.states.get("sensor.heizkostenverteiler_16660259_last_telegram_raw")
    assert hca.attributes["netzwerkknoten"] is True
    assert hca.attributes["rssi"] == -102.0 and hca.attributes["lqi"] == 0x80
    assert hca.attributes["inneres_telegramm"][2:8] == "449344"     # inneres Telegramm: C=44, Hersteller QDS
    assert "line" in hass.states.get(hca.entity_id).attributes

    # fremde Zähler: kein Sensor, nichts gespeichert
    hub = entry.runtime_data
    assert set(hub.meter_raw) == {"52144701", "16660259"}
    assert not [e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
                if "52143906" in e.unique_id]

    # Zuletzt empfangen ist gefüllt
    assert hass.states.get("sensor.warmwasser_52144701_last_received").state not in ("unknown", "unavailable")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_raw_sensor_truncated_and_unrecorded(hass: HomeAssistant, mods):
    from custom_components.maplecun.sensor import MeterRawSensor
    assert "line" in MeterRawSensor._unrecorded_attributes
    entry = _entry(hass, mods, meters=("16660259",))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    long_line = RELAYED_HCA + "C8"
    hub = entry.runtime_data
    # Rohzeile künstlich verlängern: direkt über deliver (gemeinsamer Pfad)
    from custom_components.maplecun import wmbus
    from homeassistant.util import dt as dt_util
    frame, lqi, rssi = wmbus.parse_cul_line_status(long_line)
    tele = wmbus.decode(frame)
    hub.deliver(tele, rssi, dt_util.utcnow(), "1", line=long_line + "::" + "0" * 300, lqi=lqi)
    await _settle(hass)
    st = hass.states.get("sensor.heizkostenverteiler_16660259_last_telegram_raw")
    assert len(st.state) == 255 and len(st.attributes["line"]) > 255
    await hass.config_entries.async_unload(entry.entry_id)


async def test_raw_sensor_restored(hass: HomeAssistant, mods):
    """Wert über einen Neustart: der Zähler ist bekannt (gespeichert), der Sensor stellt sich wieder her."""
    entry = _entry(hass, mods, meters=("52144701",))
    hass_storage_key = f"{DOMAIN}.{entry.entry_id}"
    from homeassistant.helpers.storage import Store
    await Store(hass, 1, hass_storage_key).async_save({
        "meter_keys": {"52144701": ["volume", "last_received"]},
        "seen": {"52144701": {"manufacturer": "LSE", "type": "Warmwasser", "module": "1",
                              "last_seen": "2026-11-14T09:00:00+01:00"}},
    })
    eid = "sensor.warmwasser_52144701_last_telegram_raw"
    mock_restore_cache(hass, [State(eid, WARM, {"line": WARM, "modul": "1", "netzwerkknoten": False,
                                                "lqi": 128, "rssi": -70.5})])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    st = hass.states.get(eid)
    assert st is not None and st.state == WARM and st.attributes["rssi"] == -70.5
    await hass.config_entries.async_unload(entry.entry_id)
