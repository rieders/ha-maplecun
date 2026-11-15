"""Batterie-Frühwarnung für Intertechno-Fernbedienungen (remote.py) – mit echten Zeilen vom 15.11."""

import pytest

from custom_components.maplecun import intertechno
from custom_components.maplecun.remote import RemoteStats, house_code, rssi_dbm

# Fernbedienung (neue Batterie, X21, Modul 2), je Taste EIN; RSSI-Byte am Ende
REAL = {
    "A": ("i55055118", "FFFF00FFFF", -62.0),
    "B": ("i55115117", "FFFF0F0FFF", -62.5),
    "C": ("i55145116", "FFFF0FF0FF", -63.0),
    "D": ("i55151110", "FFFF0FFF0F", -66.0),
}


@pytest.mark.parametrize("key", list(REAL))
def test_real_lines_code_rssi_house(key):
    line, code, dbm = REAL[key]
    assert intertechno.decode(line) == (code, "0F")
    assert rssi_dbm(intertechno.rssi_byte(line)) == dbm
    assert house_code(code) == "FFFF0"                 # alle vier Tasten: eine Fernbedienung


def test_rssi_conversion_like_fhem():
    assert rssi_dbm(0x00) == -74.0
    assert rssi_dbm(0xFE) == -75.0
    assert rssi_dbm(0x80) == -138.0


def test_median_reference_and_weak():
    st = RemoteStats()
    for _ in range(9):
        st.add_press(-62.0)
    assert st.weak(10) is None and st.reference is None          # erst ab 10 Tastendrücken
    st.add_press(-62.0)
    assert st.reference == -62.0 and st.weak(10) is False
    for _ in range(9):
        st.add_press(-75.0)
    assert st.median == -62.0 and st.weak(10) is False           # Mehrheit noch gut
    for _ in range(3):
        st.add_press(-75.0)
    assert st.median == -75.0 and st.weak(10) is True            # 13 dB unter der Referenz
    assert st.reference == -62.0                                  # Referenz sinkt nie
    assert st.weak(15) is False                                   # Schwelle einstellbar
    assert st.presses == 20                                       # gleitend über 20


def test_reference_follows_best_median():
    st = RemoteStats()
    for _ in range(10):
        st.add_press(-70.0)
    for _ in range(20):
        st.add_press(-60.0)
    assert st.reference == -60.0


def test_garbled_ratio_and_reset_and_storage():
    st = RemoteStats()
    for i in range(10):
        st.add_press(-62.0)
        if i < 7:
            st.mark_garbled()                                     # wie am 15.11.: 7 von 10
    assert st.garbled_ratio == 0.7
    restored = RemoteStats.from_dict(st.to_dict())
    assert (restored.presses, restored.reference, restored.garbled_ratio) == (10, -62.0, 0.7)
    restored.reset()
    assert restored.presses == 0 and restored.reference is None and restored.garbled_ratio is None


# ------------------------------------------------------------------ in Home Assistant
pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import device_registry as dr, entity_registry as er  # noqa: E402

from custom_components.maplecun.const import DOMAIN  # noqa: E402

from .test_init_order import _radio, _setup  # noqa: E402
from .test_maplecun import _settle, firmware, mods  # noqa: E402,F401

DEVICES = {code: {"name": name, "on": "0F", "off": "F0"} for code, name in (
    ("FFFF00FFFF", "Stehlampe"), ("FFFF0F0FFF", "Media"), ("FFFF0FF0FF", "Modellbahn"))}
BATTERY = "binary_sensor.fernbedienung_ffff0_remote_battery_low"


@pytest.fixture
def no_press_gap(monkeypatch):
    import custom_components.maplecun.hub as hub_mod
    monkeypatch.setattr(hub_mod, "IT_PRESS_GAP", 0.0)


async def _press(hass, firmware, mods, line, n=1):
    for _ in range(n):
        await _radio(firmware, mods["2"], line)
        await _settle(hass, 0.05)


async def test_one_remote_per_house_code(hass: HomeAssistant, mods, firmware, no_press_gap):
    entry = await _setup(hass, mods, it_devices=DEVICES)
    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    remotes = [d for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
               if any(i.endswith("_remote_FFFF0") for _, i in d.identifiers)]
    assert [d.name for d in remotes] == ["Fernbedienung FFFF0"]
    ids = {e.entity_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)}
    assert {BATTERY, "button.fernbedienung_ffff0_battery_replaced",
            "sensor.stehlampe_remote_signal_strength", "sensor.stehlampe_last_key_press",
            "sensor.media_remote_signal_strength", "sensor.modellbahn_last_key_press"} <= ids
    assert sum(1 for i in ids if i.startswith("binary_sensor.fernbedienung")) == 1
    assert hass.states.get(BATTERY).state == "unknown"
    # echte Zeilen: Signalstärke je Taste, Zeitstempel
    await _press(hass, firmware, mods, REAL["A"][0])
    await _press(hass, firmware, mods, REAL["B"][0])
    await _press(hass, firmware, mods, REAL["C"][0])
    assert hass.states.get("sensor.stehlampe_remote_signal_strength").state == "-62.0"
    assert hass.states.get("sensor.media_remote_signal_strength").state == "-62.5"
    assert hass.states.get("sensor.modellbahn_remote_signal_strength").state == "-63.0"
    assert hass.states.get("sensor.stehlampe_last_key_press").state not in ("unknown", "unavailable")
    assert hass.states.get(BATTERY).attributes["tastendruecke"] == 3        # alle Tasten zählen
    await hass.config_entries.async_unload(entry.entry_id)


async def test_battery_low_after_rssi_drop_and_reset(hass: HomeAssistant, mods, firmware, no_press_gap):
    entry = await _setup(hass, mods, it_devices=DEVICES)
    await _press(hass, firmware, mods, REAL["A"][0], 10)                    # -62 dBm
    st = hass.states.get(BATTERY)
    assert st.state == "off" and st.attributes["referenz_dbm"] == -62.0
    await _press(hass, firmware, mods, "i550551FE", 11)                     # -75 dBm
    st = hass.states.get(BATTERY)
    assert st.state == "on" and st.attributes["median_dbm"] == -75.0
    # gespeichert über Neustarts
    await hass.config_entries.async_reload(entry.entry_id)
    await _settle(hass, 0.8)
    assert hass.states.get(BATTERY).state == "on"
    assert hass.states.get("sensor.stehlampe_remote_signal_strength").state == "-75.0"
    # Batterie getauscht
    await hass.services.async_call("button", "press",
                                   {"entity_id": "button.fernbedienung_ffff0_battery_replaced"}, blocking=True)
    await _settle(hass)
    st = hass.states.get(BATTERY)
    assert st.state == "unknown" and st.attributes["tastendruecke"] == 0 and st.attributes["referenz_dbm"] is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_threshold_option(hass: HomeAssistant, mods, firmware, no_press_gap):
    entry = await _setup(hass, mods, it_devices=DEVICES, it_battery_db=15)
    await _press(hass, firmware, mods, REAL["A"][0], 10)
    await _press(hass, firmware, mods, "i550551FE", 11)                     # 13 dB tiefer
    assert hass.states.get(BATTERY).state == "off"
    assert hass.states.get(BATTERY).attributes["schwelle_db"] == 15
    await hass.config_entries.async_unload(entry.entry_id)


async def test_own_transmission_not_counted(hass: HomeAssistant, mods, firmware, no_press_gap):
    """Echo der eigenen Sendung (Modul 3 sendet, Modul 2 empfängt) ist kein Tastendruck."""
    entry = await _setup(hass, mods, it_devices=DEVICES)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.stehlampe"}, blocking=True)
    await _press(hass, firmware, mods, REAL["A"][0])                        # Echo
    assert hass.states.get(BATTERY).attributes["tastendruecke"] == 0
    assert hass.states.get("sensor.stehlampe_remote_signal_strength").state == "unknown"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_garbled_share_attribute_only(hass: HomeAssistant, mods, firmware, no_press_gap):
    """Verstümmelte Paketenden nach einem Druck: Attribut, löst allein nicht aus."""
    entry = await _setup(hass, mods, it_devices=DEVICES)
    for _ in range(10):
        await _press(hass, firmware, mods, REAL["A"][0])
        await _press(hass, firmware, mods, "i55155F19")                     # Phantom
    st = hass.states.get(BATTERY)
    assert st.attributes["verstuemmelt_anteil"] == 1.0
    assert st.state == "off"
    await hass.config_entries.async_unload(entry.entry_id)
