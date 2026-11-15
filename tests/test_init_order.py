"""Reihenfolge der Modul-Initialisierung: SlowRF (433 MHz) zuerst, wMBus zuletzt.

a-culfw (clib/rf_receive.c, set_txrestore): solange irgendein Modul im wMBus-Modus ist,
lädt "X21" die CC1101-Konfiguration nicht und schaltet den Empfang nicht ein. Der Simulator
(culfw_sim.Firmware) bildet das nach; am echten Gerät am 2026-11-14 bestätigt:
X21 auf Modul 2 bei laufendem S-Modus -> keine Zeile; brx, X21, brs -> Modul 2 empfängt.
"""

import asyncio
import time

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maplecun.const import DOMAIN

from .test_maplecun import _modules, _settle, firmware, mods  # noqa: F401

ROLES = ("off", "rf433", "wmbus_s", "off")        # wie die echte Installation
IT_LINE = "i0BFFC6E0"                              # Intertechno-Zeile (433 MHz)


def _broken_after_restart(fw) -> None:
    """Zustand nach HA-Neustart: S-Modus läuft noch, Modul 2 nach Intertechno-Senden stumm."""
    fw.txreport.update({1: 0x21, 2: 0x21})
    fw.mode.update({1: "slow", 2: "wmbus_s"})
    fw.mbus = "wmbus_s"
    fw.chip.update({1: "it", 2: "wmbus"})


async def _setup(hass, mods, roles=ROLES, it_devices=None, **extra):
    entry = MockConfigEntry(domain=DOMAIN, version=2, title="MapleCUN test", unique_id="127.0.0.1",
        data={"host": "127.0.0.1", "modules": _modules(mods, roles)},
        options={"init_mode": True, "meters": {}, "revolts": {}, "it_devices": it_devices or {}, **extra})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.8)
    return entry


async def _radio(fw, mod, line):
    """Funkzeile nur dann ausgeben, wenn das Modul im Simulator wirklich empfängt."""
    if fw.slow_receiving(mod.inst):
        await mod.send(line)


def _order(fw, start=0):
    return [x for x in fw.sent[start:] if x[1] in ("brx", "brs", "brt", "X21")]


def test_simulator_x21_without_effect_while_wmbus():
    """Nachbildung des Fehlers in 0.9.0: X21 allein bringt Modul 2 nicht zurück."""
    from .culfw_sim import Firmware
    fw = Firmware()
    _broken_after_restart(fw)
    fw.handle(1, "X21")
    assert not fw.slow_receiving(1)
    fw.handle(2, "brx")
    fw.handle(1, "X21")
    fw.handle(2, "brs")
    assert fw.slow_receiving(1) and fw.receiving() == {2: "wmbus_s"}


async def test_restart_with_wmbus_still_running(hass: HomeAssistant, mods, firmware):
    """Neustart/Neuladen: wMBus aus, Modul 2 initialisieren, wMBus wieder ein – beide empfangen."""
    _broken_after_restart(firmware)
    entry = await _setup(hass, mods)
    assert _order(firmware) == [(2, "brx"), (1, "X21"), (2, "X21"), (2, "brs")]
    assert firmware.slow_receiving(1)
    assert firmware.receiving() == {2: "wmbus_s"}
    await _radio(firmware, mods["2"], IT_LINE)
    await _settle(hass)
    assert entry.runtime_data.modules["2"].rx_count == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_reconnect_slowrf_while_wmbus_runs(hass: HomeAssistant, mods, firmware, caplog):
    """"Modul 2 neu verbinden" bei laufendem wMBus: wMBus kurz aus, X21, wMBus wieder ein."""
    entry = await _setup(hass, mods)
    firmware.chip[1] = "idle"                          # Modul 2 gestört
    start = len(firmware.sent)
    await hass.services.async_call("button", "press", {"entity_id": "button.maplecun_test_module_2_reconnect"},
                                   blocking=True)
    await _settle(hass, 6.5)                           # RECONNECT_DELAY = 5 s
    assert _order(firmware, start) == [(2, "brx"), (1, "X21"), (2, "X21"), (2, "brs")]
    assert firmware.slow_receiving(1) and firmware.receiving() == {2: "wmbus_s"}
    assert "wMBus auf Modul 3 kurz ausgeschaltet" in caplog.text
    await hass.config_entries.async_unload(entry.entry_id)


async def test_wmbus_reconnect_does_not_touch_slowrf(hass: HomeAssistant, mods, firmware):
    """Reconnect des wMBus-Moduls: nur dessen Modus neu setzen, SlowRF bleibt unberührt."""
    entry = await _setup(hass, mods)
    start = len(firmware.sent)
    await mods["3"].drop()
    await _settle(hass, 6.5)
    assert _order(firmware, start) == [(2, "X21"), (2, "brs")]
    assert firmware.slow_receiving(1) and firmware.receiving() == {2: "wmbus_s"}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_intertechno_send_reinitializes(hass: HomeAssistant, mods, firmware, monkeypatch):
    """Sendemodul 2 gewählt: nach "is…" stellt a-culfw SlowRF bei aktivem wMBus nicht wieder
    her -> neu initialisieren."""
    import custom_components.maplecun.hub as hub_mod
    monkeypatch.setattr(hub_mod, "IT_REINIT_DELAY", 0.05)
    entry = await _setup(hass, mods, it_devices={"FFFF0F0FFF": {"name": "Lampe", "on": "FF", "off": "F0"}},
                         it_module="2")
    start = len(firmware.sent)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.lampe"}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.lampe"}, blocking=True)
    await _settle(hass, 0.6)
    sent = [x for x in firmware.sent[start:] if x[1] in ("isFFFF0F0FFFFF", "isFFFF0F0FFFF0", "brx", "X21", "brs")]
    # zwei Sendebefehle kurz hintereinander -> einmal danach neu initialisieren
    assert sent == [(1, "isFFFF0F0FFFFF"), (1, "isFFFF0F0FFFF0"), (2, "brx"), (1, "X21"), (2, "X21"), (2, "brs")]
    assert firmware.slow_receiving(1) and firmware.receiving() == {2: "wmbus_s"}
    await hass.config_entries.async_unload(entry.entry_id)


STEHLAMPE = {"FFFF00FFFF": {"name": "Stehlampe", "on": "0F", "off": "F0"}}   # FHEM: DEF "FFFF00FFFF 0F F0"


def test_simulator_it_on_wmbus_module_restores_wmbus():
    """a-culfw: "is…" auf dem wMBus-Modul -> restore_RF_mode -> rf_mbus_init; Modul 2 unberührt."""
    from .culfw_sim import Firmware
    fw = Firmware()
    for inst, cmd in ((1, "X21"), (2, "X21"), (2, "brs")):
        fw.handle(inst, cmd)
    assert fw.handle(2, "isFFFF00FFFF0F") == ["isFFFF00FFFF0F"]
    assert fw.slow_receiving(1) and fw.receiving() == {2: "wmbus_s"}
    # Gegenprobe: über Modul 2 gesendet bleibt Modul 2 bei laufendem wMBus taub
    fw.handle(1, "isFFFF00FFFFF0")
    assert not fw.slow_receiving(1) and fw.receiving() == {2: "wmbus_s"}
    # und über ein Modul mit Rolle "Aus" endet wMBus überall (mbus_mode = WMBUS_NONE)
    fw.handle(0, "isFFFF00FFFFF0")
    assert fw.receiving() == {}


async def test_intertechno_auto_sends_via_wmbus_module(hass: HomeAssistant, mods, firmware):
    """Vorgabe wie FHEM (IODev = wMBus-Modul): senden über Modul 3, keine Neuinitialisierung,
    433-MHz-Empfang auf Modul 2 und wMBus S auf Modul 3 laufen weiter."""
    entry = await _setup(hass, mods, it_devices=STEHLAMPE)
    hub = entry.runtime_data
    assert hub.it_module() == "3" and hub.it_module_choices() == ["2", "3"]
    start = len(firmware.sent)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.stehlampe"}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.stehlampe"}, blocking=True)
    await _settle(hass, 1.5)
    assert firmware.sent[start:] == [(2, "isFFFF00FFFF0F"), (2, "isFFFF00FFFFF0")]
    assert firmware.slow_receiving(1) and firmware.receiving() == {2: "wmbus_s"}
    assert hass.states.get("switch.stehlampe").state == "off"
    # Bestätigung der Firmware zählt nicht als Empfang
    assert hub.modules["3"].rx_count == 0
    # Fernbedienung wird auf Modul 2 weiter empfangen
    from custom_components.maplecun import intertechno
    await _radio(firmware, mods["2"], intertechno.encode("FFFF00FFFF", "0F") + "1A")
    await _settle(hass)
    assert hass.states.get("switch.stehlampe").state == "on"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_intertechno_auto_falls_back_to_433_module(hass: HomeAssistant, mods, firmware):
    """Ohne wMBus-Modul sendet "auto" über das 433-MHz-Modul; Module mit Rolle "Aus" nie."""
    entry = await _setup(hass, mods, roles=("off", "rf433", "off", "off"), it_devices=STEHLAMPE, it_module="3")
    assert entry.runtime_data.it_module_choices() == ["2"]
    assert entry.runtime_data.it_module() == "2"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_intertechno_auto_uses_433_module_while_wmbus_disconnected(hass: HomeAssistant, mods, firmware):
    entry = await _setup(hass, mods, it_devices=STEHLAMPE)
    hub = entry.runtime_data
    hub.modules["3"].connected = False
    assert hub.it_module() == "2"
    hub.modules["3"].connected = True
    await hass.config_entries.async_unload(entry.entry_id)


async def test_intertechno_without_wmbus_sends_only(hass: HomeAssistant, mods, firmware):
    """Ohne wMBus stellt die Firmware SlowRF selbst wieder her – nichts zusätzlich senden."""
    entry = await _setup(hass, mods, roles=("off", "rf433", "off", "off"),
                         it_devices={"FFFF0F0FFF": {"name": "Lampe", "on": "FF", "off": "F0"}})
    start = len(firmware.sent)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.lampe"}, blocking=True)
    await _settle(hass, 1.5)
    assert [c for _, c in firmware.sent[start:]] == ["isFFFF0F0FFFFF"]
    assert firmware.slow_receiving(1)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_slowrf_watchdog(hass: HomeAssistant, mods, firmware, caplog):
    """15 min ohne Zeile auf dem 433-MHz-Modul -> einmal neu initialisieren, max. 1x/h."""
    entry = await _setup(hass, mods)
    hub = entry.runtime_data
    firmware.chip[1] = "idle"
    start = len(firmware.sent)
    hub.modules["2"].last_line = time.monotonic() - 16 * 60
    hub.modules["3"].last_line = time.monotonic()      # wMBus nicht betroffen
    await hub._async_watchdog()
    await _settle(hass)
    assert _order(firmware, start) == [(2, "brx"), (1, "X21"), (2, "X21"), (2, "brs")]
    assert firmware.slow_receiving(1)
    assert "Modul 2: seit 16 min keine Zeile empfangen – SlowRF wird neu initialisiert" in caplog.text
    n = len(firmware.sent)
    hub.modules["2"].last_line = time.monotonic() - 20 * 60
    await hub._async_watchdog()
    await _settle(hass)
    assert len(firmware.sent) == n                     # innerhalb einer Stunde nicht erneut
    await hass.config_entries.async_unload(entry.entry_id)


async def test_concurrent_inits_are_serialized(hass: HomeAssistant, mods, firmware):
    """Gleichzeitige Anstöße (Wächter, Optionen, Reconnect) laufen nacheinander über eine Sperre."""
    entry = await _setup(hass, mods)
    hub = entry.runtime_data
    start = len(firmware.sent)
    await asyncio.gather(
        hub.async_set_mode("3", "Test A"), hub.async_init_slowrf(["2"], "Test B"),
        hub.async_set_mode("2", "Test C"), hub.async_set_mode("3", "Test D"),
    )
    await _settle(hass)
    seq = _order(firmware, start)
    # Blöcke greifen nicht ineinander: nach jedem brx folgt X21 (Modul 2) vor dem nächsten brs
    for i, (inst, cmd) in enumerate(seq):
        if cmd == "brx":
            assert seq[i + 1] == (1, "X21")
    assert firmware.slow_receiving(1) and firmware.receiving() == {2: "wmbus_s"}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_reads_do_not_change_reception(hass: HomeAssistant, mods, firmware):
    """V, ?, Cxx, X, t sind rein lesend (fncollection.c version, multi_CC.c callfn2,
    cc1100.c ccreg, rf_receive.c set_txreport ohne Argument, clock.c gettime)."""
    entry = await _setup(hass, mods)
    before = (dict(firmware.mode), dict(firmware.chip), firmware.mbus)
    start = len(firmware.sent)
    await entry.runtime_data.async_refresh_all()
    await _settle(hass)
    sent = {c for _, c in firmware.sent[start:]}
    assert sent <= {"V", "?", "X", "t", "C0D", "C0E", "C0F", "C10", "C1B", "C1D"}
    assert (dict(firmware.mode), dict(firmware.chip), firmware.mbus) == before
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_it_module(hass: HomeAssistant, mods, firmware):
    """Einstellungen: Sendemodul wählbar (auto, 2, 3); Module mit Rolle "Aus" werden nicht angeboten."""
    entry = await _setup(hass, mods, it_devices=STEHLAMPE)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "settings"})
    sel = next(v for k, v in r["data_schema"].schema.items() if k == "it_module")
    assert sel.config["options"] == ["auto", "2", "3"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {
        "auto_revolt": True, "auto_it": True, "auto_sensor": False, "it_module": "2"})
    assert r["type"] == "create_entry" and entry.options["it_module"] == "2"
    await _settle(hass, 1.0)                           # Optionen -> Neuladen
    assert entry.runtime_data.it_module() == "2"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_it_switch_shows_commands_and_hints_wrong_suffix(hass: HomeAssistant, mods, firmware, caplog):
    """Stehlampe mit Vorgabe-Endung FF: Attribute zeigen den gesendeten Befehl, empfangenes 0F
    erzeugt einmal einen Hinweis im Log."""
    entry = await _setup(hass, mods, it_devices={"FFFF00FFFF": {"name": "Stehlampe", "on": "FF", "off": "F0"}})
    attrs = hass.states.get("switch.stehlampe").attributes
    assert attrs["befehl_ein"] == "isFFFF00FFFFFF" and attrs["befehl_aus"] == "isFFFF00FFFFF0"
    assert attrs["sendemodul"] == "3"
    from custom_components.maplecun import intertechno
    for _ in range(2):
        await _radio(firmware, mods["2"], intertechno.encode("FFFF00FFFF", "0F") + "1A")
        await _settle(hass)
    assert caplog.text.count("Fernbedienung sendet Befehl 0F") == 1
    await hass.config_entries.async_unload(entry.entry_id)


def test_adopt_uses_received_on_suffix():
    from custom_components.maplecun.hub import it_cmds_from_seen
    assert it_cmds_from_seen({"cmd": "F0", "cmds": ["0F", "F0"]}) == {"on": "0F", "off": "F0"}
    assert it_cmds_from_seen({"cmd": "FF"}) == {"on": "FF", "off": "F0"}


# Echte Zeilen der Fernbedienung (Taste A = Stehlampe, neue Batterie, X21), 2026-11-15
REMOTE_ON = ["i55055118", "i55155F19"]           # EIN, ~0,5 s später das abgeschnittene Paketende
REMOTE_OFF = ["i55055416"]                       # AUS (aufgezeichnet mit X27)


async def test_remote_sets_state_without_sending_and_drops_phantom(hass: HomeAssistant, mods, firmware):
    """Statusrückmeldung: empfangener Code setzt den Schalter, ohne zu senden; das
    verstümmelte Paketende (FFFF0FFFFF/11) wird weder ausgewertet noch als Gerät gemeldet."""
    entry = await _setup(hass, mods, it_devices=STEHLAMPE)
    hub = entry.runtime_data
    start = len(firmware.sent)
    assert hass.states.get("switch.stehlampe").state == "off"
    for line in REMOTE_ON:
        await _radio(firmware, mods["2"], line)
    await _settle(hass)
    assert hass.states.get("switch.stehlampe").state == "on"
    assert not hub.discovered                          # kein Phantom-Gerät FFFF0FFFFF
    await asyncio.sleep(1.6)                           # außerhalb des Fensters
    for line in REMOTE_OFF:
        await _radio(firmware, mods["2"], line)
    await _settle(hass)
    assert hass.states.get("switch.stehlampe").state == "off"
    assert not [c for _, c in firmware.sent[start:] if c.startswith("is")]   # nichts gesendet
    await hass.config_entries.async_unload(entry.entry_id)


async def test_unknown_code_alone_is_still_discovered(hass: HomeAssistant, mods, firmware):
    """Der Filter greift nur direkt nach einem anderen Code – ein einzeln empfangener
    fremder Code erscheint weiterhin unter "Gefundene Geräte"."""
    entry = await _setup(hass, mods, it_devices=STEHLAMPE)
    await _radio(firmware, mods["2"], "i55155F19")
    await _settle(hass)
    assert "it:FFFF0FFFFF" in entry.runtime_data.discovered
    await hass.config_entries.async_unload(entry.entry_id)
