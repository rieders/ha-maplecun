"""Modulwerte, Einstellungen und send_raw in Home Assistant – nur gegen den simulierten MapleCUN."""

import logging
import time

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maplecun.const import DOMAIN

from .test_maplecun import _modules, _settle, firmware, mods  # noqa: F401

P = "maplecun_test"


async def _setup(hass, mods, roles=("wmbus_t", "rf433", "off", "monitor"), enable=()):
    entry = MockConfigEntry(domain=DOMAIN, version=2, title="MapleCUN test", unique_id="127.0.0.1",
        data={"host": "127.0.0.1", "modules": _modules(mods, roles)},
        options={"init_mode": True, "meters": {}, "revolts": {}, "it_devices": {}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    if enable:
        reg = er.async_get(hass)
        for eid in enable:
            assert reg.async_get(eid) is not None, eid
            reg.async_update_entity(eid, disabled_by=None)
        await hass.config_entries.async_reload(entry.entry_id)
        await _settle(hass, 0.6)
    return entry


def _writes(firmware) -> list[tuple[int, str]]:
    return [(i, c) for i, c in firmware.sent if c[0] in "WxlBe" or c.startswith("ic")]


async def test_module_values_on_connect(hass: HomeAssistant, mods, firmware):
    entry = await _setup(hass, mods)
    st = lambda e: hass.states.get(f"sensor.{P}_{e}")  # noqa: E731
    assert st("firmware_module_1").state.startswith("1.26.08 a-culfw Build: 323")
    assert "b" in st("commands_module_1").state.split()
    assert float(st("frequency_module_2").state) == 868.3
    assert st("frequency_module_2").attributes["unit_of_measurement"] == "MHz"
    assert float(st("bandwidth_module_2").state) == 325
    assert st("amplification_module_2").state == "42" and st("sensitivity_module_2").state == "4"
    assert st("send_credit_module_2").state == "900"
    assert float(st("uptime_module_1").state) == pytest.approx(987 / 86400, abs=0.01)   # Tage
    assert entry.runtime_data.modules["2"].cmds and "t" not in entry.runtime_data.modules["2"].cmds
    assert mods["4"].received == []                         # "Nur überwachen": nichts von sich aus
    assert _writes(firmware) == []                          # beim Start nie schreiben
    await hass.config_entries.async_unload(entry.entry_id)


async def test_refresh_button_rate_limit(hass: HomeAssistant, mods, firmware):
    entry = await _setup(hass, mods)
    hub = entry.runtime_data
    n_x = mods["2"].received.count("X")
    await hass.services.async_call("button", "press", {"entity_id": f"button.{P}_refresh_module_values"}, blocking=True)
    await _settle(hass)
    assert mods["2"].received.count("C0D") >= 2              # ccconf neu gelesen
    assert mods["2"].received.count("X") == n_x              # credit10ms: höchstens alle 15 min
    hub.modules["2"].info_at["slow"] = time.monotonic() - 16 * 60
    firmware.credit = 123
    await hass.services.async_call("button", "press", {"entity_id": f"button.{P}_refresh_module_values"}, blocking=True)
    await _settle(hass)
    assert mods["2"].received.count("X") == n_x + 1
    assert hass.states.get(f"sensor.{P}_send_credit_module_2").state == "123"
    assert _writes(firmware) == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_settings_only_for_433_module(hass: HomeAssistant, mods, firmware):
    """wMBus-Modul: keine Funkeinstellungen; 433-MHz-Modul: vorhanden, aber deaktiviert."""
    entry = await _setup(hass, mods)
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_set_freq_1") is None
    assert reg.async_get_entity_id("select", DOMAIN, f"{entry.entry_id}_set_bwidth_1") is None
    assert reg.async_get_entity_id("select", DOMAIN, f"{entry.entry_id}_set_patable_1") is None
    for uid in ("set_freq_2", "set_itclock_2"):
        ent = reg.async_get(reg.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_{uid}"))
        assert ent.disabled_by == er.RegistryEntryDisabler.INTEGRATION and ent.entity_category == "config"
    for uid in ("set_bwidth_2", "set_rampl_2", "set_sens_2", "set_led_1"):
        ent = reg.async_get(reg.async_get_entity_id("select", DOMAIN, f"{entry.entry_id}_{uid}"))
        assert ent.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    assert reg.async_get_entity_id("select", DOMAIN, f"{entry.entry_id}_set_patable_2") is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_write_settings_backup_first(hass: HomeAssistant, mods, firmware, caplog):
    """Nur auf Nutzeraktion; vor dem ersten Schreiben ccconf ins Log; danach X21 zum Laden."""
    caplog.set_level(logging.INFO, logger="custom_components.maplecun")
    ents = [f"number.{P}_set_frequency_module_2", f"select.{P}_set_bandwidth_module_2",
            f"select.{P}_set_amplification_module_2", f"select.{P}_set_sensitivity_module_2",
            f"number.{P}_intertechno_clock_module_2"]
    entry = await _setup(hass, mods, roles=("off", "rf433", "off", "off"), enable=ents)
    assert _writes(firmware) == []                          # Wiederherstellen schreibt nichts
    assert hass.states.get(ents[0]).state == "868.3"

    await hass.services.async_call("number", "set_value", {"entity_id": ents[0], "value": 868.35}, blocking=True)
    await _settle(hass)
    assert "Sicherung vor dem Schreiben ins EEPROM – freq 868.300 MHz, bWidth 325 kHz" in caplog.text
    assert "C0D=21 C0E=65 C0F=6A C10=57 C1B=07 C1D=90" in caplog.text
    w = [c for i, c in _writes(firmware) if i == 1]
    assert w == ["W0F21", "W1065", "W11E8"]
    after_w = mods["2"].received[mods["2"].received.index("W11E8"):]
    assert "X21" in after_w                                  # Neu laden wie FHEM CUL_WriteInit
    assert hass.states.get(ents[0]).state == "868.35"        # aus ccconf zurückgelesen

    await hass.services.async_call("select", "select_option", {"entity_id": ents[1], "option": "203"}, blocking=True)
    await hass.services.async_call("select", "select_option", {"entity_id": ents[2], "option": "38"}, blocking=True)
    await hass.services.async_call("select", "select_option", {"entity_id": ents[3], "option": "8"}, blocking=True)
    await _settle(hass)
    w = [c for i, c in _writes(firmware) if i == 1]
    assert w[3:] == ["W1287", "W1D05", "W1F91"]
    assert caplog.text.count("Sicherung vor dem Schreiben") == 1        # nur vor dem ersten Schreiben
    assert hass.states.get(ents[1]).state == "203"
    assert hass.states.get(ents[2]).state == "38" and hass.states.get(ents[3]).state == "8"

    await hass.services.async_call("number", "set_value", {"entity_id": ents[4], "value": 300}, blocking=True)
    await _settle(hass)
    assert "ic300" in mods["2"].received
    await hass.config_entries.async_unload(entry.entry_id)


async def test_write_while_wmbus_active_warns(hass: HomeAssistant, mods, firmware, caplog):
    ents = [f"select.{P}_set_sensitivity_module_2"]
    entry = await _setup(hass, mods, enable=ents)          # Modul 1 im T-Modus
    await hass.services.async_call("select", "select_option", {"entity_id": ents[0], "option": "12"}, blocking=True)
    await _settle(hass)
    assert (1, "W1F92") in firmware.sent
    assert "erst nach einem Neustart des MapleCUN" in caplog.text
    assert firmware.receiving() == {0: "wmbus_t"}            # wMBus-Empfang unberührt
    await hass.config_entries.async_unload(entry.entry_id)


async def test_patable_and_led_module_1(hass: HomeAssistant, mods, firmware):
    ents = [f"select.{P}_transmit_power_module_1", f"select.{P}_led_module_1"]
    entry = await _setup(hass, mods, roles=("rf433", "off", "off", "off"), enable=ents)
    await hass.services.async_call("select", "select_option", {"entity_id": ents[0], "option": "07"}, blocking=True)
    await hass.services.async_call("select", "select_option", {"entity_id": ents[1], "option": "blink"}, blocking=True)
    await _settle(hass)
    assert [c for _, c in _writes(firmware)] == ["x07", "l02"]
    assert hass.states.get(ents[1]).state == "blink"
    # nach dem Neuladen nur anzeigen, nicht erneut senden
    await hass.config_entries.async_reload(entry.entry_id)
    await _settle(hass, 0.6)
    assert [c for _, c in _writes(firmware)] == ["x07", "l02"]
    assert hass.states.get(ents[1]).state == "blink"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_send_raw(hass: HomeAssistant, mods, firmware):
    entry = await _setup(hass, mods, enable=[f"sensor.{P}_raw_data_module_2"])
    r = await hass.services.async_call(DOMAIN, "send_raw", {"module": "2", "command": "C0D"},
                                       blocking=True, return_response=True)
    assert r == {"module": "2", "command": "C0D", "response": "C0D = 21 / 33"}
    await _settle(hass)
    assert hass.states.get(f"sensor.{P}_raw_data_module_2").state == "C0D -> C0D = 21 / 33"
    for cmd in ("B01", "e"):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(DOMAIN, "send_raw", {"module": "1", "command": cmd},
                                           blocking=True, return_response=True)
    assert not any(c in ("B01", "e") for c in mods["1"].received)
    r = await hass.services.async_call(DOMAIN, "send_raw", {"module": "1", "command": "B01", "force": True,
                                                            "timeout": 0.5}, blocking=True, return_response=True)
    assert r["response"] is None and "B01" in mods["1"].received      # nur simuliert!
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "send_raw", {"module": "3", "command": "V"},
                                       blocking=True, return_response=True)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_reopen_button(hass: HomeAssistant, mods, firmware, monkeypatch):
    from custom_components.maplecun import hub as hub_mod
    monkeypatch.setattr(hub_mod, "RECONNECT_DELAY", 0.1)
    entry = await _setup(hass, mods)
    await hass.services.async_call("button", "press", {"entity_id": f"button.{P}_reconnect_module_1"}, blocking=True)
    await _settle(hass, 1.0)
    assert mods["1"].received.count("brt") == 2 and firmware.mode[0] == "wmbus_t"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_offer_only_supported_roles(hass: HomeAssistant, mods, firmware):
    entry = await _setup(hass, mods, roles=("wmbus_t", "rf433", "off", "monitor"))
    await hass.services.async_call("button", "press", {"entity_id": f"button.{P}_refresh_module_values"}, blocking=True)
    await _settle(hass)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "modules"})
    opts = {str(k): v.config["options"] for k, v in r["data_schema"].schema.items() if str(k).startswith("role_")}
    assert opts["role_4"] == ["monitor", "off"]               # fntab3: kein b, kein i
    assert "wmbus_s" in opts["role_1"] and "rf433" in opts["role_2"]
    await hass.config_entries.async_unload(entry.entry_id)
