import asyncio
import logging
import re

import pytest

pytest.importorskip("homeassistant")

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maplecun.const import DOMAIN
from custom_components.maplecun import wmbus

from .culfw_sim import Firmware

REVOLT = "r3a65E6006432" "08FC" "64" "04D2"   # 230V 1.00A 50Hz 230.0W pf1.00 12.34kWh
# echtes Telegramm Warmwasser 52144701 (+ LQI 80, RSSI 07 wie von a-culfw angehängt)
WARM = "b2D446532014714521A06B78D7AE40000000C13297102004C13429900370400426C3F3C02BB560000326CFFFF046D30A30D08443A43758007"
# echte Telegramme über Netzwerkknoten (CI A0): Kaltwasser 52143906 bzw. HKV 16660259
RELAYED_HCA = ("b54C465B276904000F0BA583AA0831000B059C3003144934459026616E7C536087AA30000200B6E88"
               "04004B6E5102038300426C3F3CCB086E860400C2086C5F3A1EAE326CFFFF046D170D413B2F04ED39"
               "1F0B2B86283801FD0C1002FD3CC201356980")
REPEATED = "b4BC465B276908000F13178A7A0831100AB327001394465320639145209761A077ACD0000000C13078303004C13615B7A350100426C3F3C02BB560000326CFFFFB234046D0A095E3982046C5F388C04135254A1DC0300DFF28007"


class FakeModule:
    """Ein Port des Split-Proxys; antwortet wie a-culfw (culfw_sim)."""

    def __init__(self, firmware: Firmware | None = None, inst: int = 0):
        self.received: list[str] = []
        self.clients: list[asyncio.StreamWriter] = []
        self.firmware = firmware
        self.inst = inst

    async def start(self, port: int = 0):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        self.clients.append(writer)
        while line := await reader.readline():
            cmd = line.decode().strip()
            self.received.append(cmd)
            if self.firmware is not None:
                for reply in self.firmware.handle(self.inst, cmd):
                    await self.send(reply)

    async def send(self, line: str):
        for w in list(self.clients):
            if w.is_closing():
                self.clients.remove(w)
                continue
            try:
                w.write(line.encode() + b"\r\n")
                await w.drain()
            except ConnectionError:
                self.clients.remove(w)

    async def drop(self):
        """Verbindungen trennen (Server läuft weiter) – simuliert einen Reconnect."""
        for w in self.clients:
            w.close()
        self.clients.clear()

    async def stop(self):
        await self.drop()
        self.server.close()


@pytest.fixture
def firmware():
    return Firmware()


@pytest.fixture
async def mods(socket_enabled, firmware):
    m = {k: FakeModule(firmware, i) for i, k in enumerate("1234")}
    for x in m.values():
        await x.start()
    yield m
    for x in m.values():
        await x.stop()


async def _settle(hass, t=0.3):
    for _ in range(int(t / 0.05)):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()


def _cmds(mod) -> list[str]:
    """Gesendete Befehle ohne die lesenden Modulabfragen (Cxx, X, t)."""
    return [c for c in mod.received if not re.fullmatch(r"C[0-9A-F]{2}|X|t", c)]


def _modules(mods, roles=("wmbus_t", "rf433", "off", "monitor")):
    return {k: {"port": mods[k].port, "role": r} for k, r in zip("1234", roles)}


# ------------------------------------------------------------------ Dekoder
def test_decoder_real_telegrams():
    frame, rssi = wmbus.parse_cul_line(WARM)
    t = wmbus.decode(frame)
    assert (t.manufacturer, t.meter_id, t.type_name) == ("LSE", "52144701", "Warmwasser")
    v = t.values()
    assert v["volume"] == 27.129 and v["volume_s1"] == 9.942
    assert str(v["date_s1"]) == "2025-12-31" and str(v["datetime"]) == "2026-10-04 08:13:00"
    assert rssi == -70.5          # wie FHEM

    t2 = wmbus.decode(wmbus.parse_cul_line(REPEATED)[0])
    assert t2.meter_id == "52143906" and t2.relayed
    assert t2.values()["volume"] == 38.307

    with pytest.raises(wmbus.WMBusError):
        wmbus.parse_cul_line(WARM[:40] + "00" + WARM[42:])     # CRC kaputt


def test_aes_mode5_roundtrip():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    frame, _ = wmbus.parse_cul_line(WARM)
    key = bytes(range(16))
    acc = frame[11]
    plain = b"\x2f\x2f" + frame[15:]
    plain += b"\x2f" * (-len(plain) % 16)
    iv = frame[2:4] + frame[4:10] + bytes([acc]) * 8
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(plain) + enc.finalize()
    cfg = bytes([(len(ct) // 16) << 4, 0x05])
    body = frame[1:13] + cfg + ct
    tele = bytes([len(body)]) + body
    t = wmbus.decode(tele)                       # ohne Schlüssel
    assert t.encrypted and not t.records
    t = wmbus.decode(tele, {"52144701": key})   # mit Schlüssel
    assert t.values()["volume"] == 27.129


# ------------------------------------------------------------------ Config Flow
async def test_config_flow(hass: HomeAssistant, mods):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.FORM
    data = {"host": "127.0.0.1", "it_codes": "ffff0f0fff"}
    for k in "1234":
        data[f"port_{k}"] = mods[k].port
    data.update(role_1="wmbus_t", role_2="rf433", role_3="off", role_4="monitor")
    bad = {**data, "port_4": 1}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], bad)
    assert r["errors"] == {"port_4": "cannot_connect"}
    # a-culfw: nur ein Modul gleichzeitig im wMBus-Modus
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {**data, "role_3": "wmbus_s"})
    assert r["errors"] == {"base": "wmbus_exclusive"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], data)
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"]["modules"]["2"]["role"] == "rf433"
    assert "FFFF0F0FFF" in r["options"]["it_devices"]
    await _settle(hass, 0.6)
    # SlowRF zuerst (wMBus kann vom letzten Lauf noch laufen -> brx), wMBus zuletzt
    assert _cmds(mods["1"])[:5] == ["V", "?", "brx", "X21", "brt"]
    assert _cmds(mods["2"])[:3] == ["V", "?", "X21"]
    assert mods["4"].received == []           # monitor: nichts senden
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ Betrieb
from custom_components.maplecun import intertechno


async def test_full(hass: HomeAssistant, mods, caplog):
    entry = MockConfigEntry(domain=DOMAIN, version=2, title="MapleCUN test", unique_id="127.0.0.1",
        data={"host": "127.0.0.1", "modules": _modules(mods)},
        options={"init_mode": True, "meters": {}, "revolts": {},
                 "it_devices": {"FFFF0F0FFF": {"name": "Steckdose Flur", "on": "FF", "off": "F0"}}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)

    assert hass.states.get("binary_sensor.maplecun_test_module_1").state == "on"
    assert hass.states.get("binary_sensor.maplecun_test_module_4").state == "on"

    # Intertechno über Modul 2 schalten
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.steckdose_flur"}, blocking=True)
    await _settle(hass)
    assert "isFFFF0F0FFFFF" in mods["2"].received
    # Wandschalter mit gleichem Code drückt "Aus" -> Zustand folgt
    await mods["2"].send(intertechno.encode("FFFF0F0FFF", "F0") + "1A")
    await _settle(hass)
    assert hass.states.get("switch.steckdose_flur").state == "off"

    # Revolt: automatisch übernommen (Vorgabe) -> Neuladen -> Werte
    await mods["2"].send(REVOLT)
    await _settle(hass, 1.2)
    assert "3a65" in entry.options["revolts"]
    await mods["2"].send(REVOLT)
    await _settle(hass)
    assert float(hass.states.get("sensor.revolt_3a65_power").state) == 230.0

    # Unbekannte Fernbedienung -> "gefunden", Benachrichtigung; unbekannte Zähler -> verworfen
    await mods["2"].send(intertechno.encode("0000FFFF00", "FF") + "20")
    await mods["1"].send(WARM)
    await mods["1"].send(REPEATED)
    await _settle(hass)
    hub = entry.runtime_data
    assert set(hub.discovered) == {"it:0000FFFF00"}
    assert hub.seen == {}                         # Zähler der Nachbarn werden nicht gespeichert
    from homeassistant.components import persistent_notification as pn
    assert any("neue Geräte" in (n.get("message") or "") for n in pn._async_get_or_create_notifications(hass).values())
    # Zählerübersicht: fremde Zähler nur mit Kopfdaten, nur im Arbeitsspeicher
    assert hass.states.get("sensor.maplecun_test_received_meters").state == "2"
    assert set(hub.heard) == {"52144701", "52143906"}
    assert "Unbekannter Zähler" not in caplog.text   # Diagnose-Log standardmäßig aus

    # Gefundene Geräte: nichts übernehmen, Rest ignorieren
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["menu_options"][0] == "discovered"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "discovered"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"adopt": [], "ignore_rest": True})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await _settle(hass, 1.0)
    assert entry.runtime_data.discovered == {} and "it:0000FFFF00" in entry.runtime_data.ignored

    # Zähler-IDs eintragen (Warmwasser direkt, HKV über Netzwerkknoten)
    for meter_id in ("52144701", "16660259"):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "add_meter"})
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"meter_id": meter_id})
        assert r["type"] is FlowResultType.CREATE_ENTRY
        await _settle(hass, 1.0)
    hub = entry.runtime_data
    assert set(entry.options["meters"]) == {"52144701", "16660259"}

    await mods["1"].send(WARM)
    await mods["1"].send(RELAYED_HCA)
    await mods["1"].send(REPEATED)                # weiterhin unbekannt
    await _settle(hass)
    assert set(hub.seen) == {"52144701", "16660259"}
    ents = er.async_get(hass)
    st = hass.states.get("sensor.warmwasser_52144701_meter_reading")
    assert float(st.state) == 27.129
    assert st.attributes["device_class"] == "water" and st.attributes["state_class"] == "total_increasing"
    assert st.attributes["unit_of_measurement"] == "m³"
    assert float(hass.states.get("sensor.warmwasser_52144701_due_date_value").state) == 9.942
    assert ents.async_get("sensor.warmwasser_52144701_due_date_value").entity_category == "diagnostic"
    assert hass.states.get("sensor.warmwasser_52144701_due_date").state == "2025-12-31"
    assert float(hass.states.get("sensor.warmwasser_52144701_signal_strength").state) == -70.5
    last = hass.states.get("sensor.warmwasser_52144701_last_received")
    assert last.state not in ("unknown", "unavailable")
    assert ents.async_get(last.entity_id).entity_category == "diagnostic"

    hca = hass.states.get("sensor.heizkostenverteiler_16660259_consumption_units")
    assert hca.state == "488" and hca.attributes["state_class"] == "total_increasing"
    assert "unit_of_measurement" not in hca.attributes
    assert hass.states.get("sensor.heizkostenverteiler_16660259_due_date_value").state == "251"
    month = hass.states.get("sensor.heizkostenverteiler_16660259_month_end_value")
    assert month.state == "486" and ents.async_get(month.entity_id).entity_category == "diagnostic"
    assert hass.states.get("sensor.heizkostenverteiler_16660259_month_end").state == "2026-10-31"
    dev = next(d for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
               if (DOMAIN, f"{entry.entry_id}_meter_16660259") in d.identifiers)
    assert (dev.manufacturer, dev.model, dev.serial_number) == ("QDS", "Heizkostenverteiler", "16660259")

    # Zähler umbenennen
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "edit_meter"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"meter_id": "52144701"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"name": "Warmwasser Bad", "key": ""})
    await _settle(hass, 1.0)
    assert float(hass.states.get("sensor.warmwasser_52144701_meter_reading").state) == 27.129
    devs = {d.name for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)}
    assert {"Warmwasser Bad", "Revolt 3a65", "Steckdose Flur", "MapleCUN test"} <= devs

    # Einstellungen: Ignorierte zurücksetzen
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "settings"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"auto_revolt": True, "auto_it": False, "auto_sensor": False, "log_unknown_meters": True, "reset_ignored": True})
    await _settle(hass, 1.0)
    caplog.set_level(logging.INFO, logger="custom_components.maplecun")
    await mods["1"].send(REPEATED)
    await _settle(hass)
    assert "Unbekannter Zähler 52143906" in caplog.text
    assert entry.runtime_data.discovered == {} and "52143906" not in entry.runtime_data.seen
    assert "52143906" not in entry.runtime_data.meter_keys

    # Zähler entfernen -> Gerät weg
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "remove_meter"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"meters": ["52144701"]})
    await _settle(hass, 1.0)
    devs = {d.name for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)}
    assert "Warmwasser Bad" not in devs

    # Modul getrennt
    await mods["2"].stop()
    await _settle(hass, 0.5)
    assert hass.states.get("binary_sensor.maplecun_test_module_2").state == "off"
    assert hass.states.get("switch.steckdose_flur").state == "unavailable"
    # Zähler senden selten: Wert bleibt erhalten
    assert hass.states.get("sensor.heizkostenverteiler_16660259_consumption_units").state == "488"
    await hass.config_entries.async_unload(entry.entry_id)


def _cul_frame_a(tele_hex: str, status: str = "80") -> str:
    """CRC-freies Telegramm als a-culfw-Zeile (Rahmen A mit Block-CRCs) verpacken."""
    tele = bytes.fromhex(tele_hex)
    blocks = [tele[:10]] + [tele[i:i + 16] for i in range(10, len(tele), 16)]
    raw = b"".join(b + wmbus.crc16(b).to_bytes(2, "big") for b in blocks)
    return "b" + raw.hex().upper() + status


# wmbusmeters simulations/serial_aes.msg: Kamstrup Multical 21, ELL 8D, AES-CTR
KAMSTRUP = "2A442D2C998734761B168D2091D37CAC21E1D68CDAFFCD3DC452BD802913FF7B1706CA9E355D6C2701CC24"


async def test_encrypted_meter_key_via_options(hass: HomeAssistant, mods, caplog):
    entry = MockConfigEntry(domain=DOMAIN, version=2, title="MapleCUN test", unique_id="127.0.0.1",
        data={"host": "127.0.0.1", "modules": _modules(mods)},
        options={"init_mode": True, "meters": {}, "revolts": {}, "it_devices": {}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)

    # falscher Schlüssel -> abgelehnt, Warnung im Log
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "add_meter"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"meter_id": "76348799", "key": "00" * 16})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await _settle(hass, 1.0)
    await mods["1"].send(_cul_frame_a(KAMSTRUP))
    await _settle(hass)
    assert "Zähler 76348799: Payload-CRC (ELL) falsch" in caplog.text
    assert hass.states.get("sensor.kaltwasser_76348799_meter_reading") is None

    # richtigen Schlüssel eintragen
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "edit_meter"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"meter_id": "76348799"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"name": "", "key": "28F64A24988064A079AA2C807D6102AE"})
    await _settle(hass, 1.0)
    await mods["1"].send(_cul_frame_a(KAMSTRUP))
    await _settle(hass)
    st = hass.states.get("sensor.kaltwasser_76348799_meter_reading")
    assert float(st.state) == 6.408 and st.attributes["device_class"] == "water"
    await hass.config_entries.async_unload(entry.entry_id)


async def _setup(hass, mods, roles=("wmbus_t", "rf433", "off", "monitor"), init=True):
    entry = MockConfigEntry(domain=DOMAIN, version=2, title="MapleCUN test", unique_id="127.0.0.1",
        data={"host": "127.0.0.1", "modules": _modules(mods, roles)},
        options={"init_mode": init, "meters": {}, "revolts": {}, "it_devices": {}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    return entry


async def test_wmbus_mode_acknowledged(hass: HomeAssistant, mods, firmware):
    """brt -> TMODE: Antwort ausgewertet, im Diagnose-Sensor sichtbar."""
    entry = await _setup(hass, mods)
    assert _cmds(mods["1"]) == ["V", "?", "brx", "X21", "brt"]
    assert firmware.mode[0] == "wmbus_t"
    st = hass.states.get("sensor.maplecun_test_receive_mode_module_1")
    assert st.state == "TMODE"
    assert st.attributes["befehl"] == "brt" and st.attributes["firmware"].startswith("V 1.26.08 a-culfw")
    assert "b" in st.attributes["kommandos"]
    assert hass.states.get("sensor.maplecun_test_receive_mode_module_2") is None   # nur wMBus-Module
    await hass.config_entries.async_unload(entry.entry_id)


async def test_wmbus_mode_idempotent_after_reconnect(hass: HomeAssistant, mods, firmware, monkeypatch):
    """Nach jedem Verbinden wird der Modus neu gesetzt – egal, was vorher war."""
    from custom_components.maplecun import hub as hub_mod
    monkeypatch.setattr(hub_mod, "RECONNECT_DELAY", 0.1)
    entry = await _setup(hass, mods)
    firmware.mode[0] = "off"                         # z. B. Neustart des MapleCUN / anderer Client
    await mods["1"].drop()
    await _settle(hass, 1.0)
    assert mods["1"].received.count("brt") == 2
    assert firmware.mode[0] == "wmbus_t"
    # brt bei schon aktivem T-Modus: bleibt an (kein Umschalten)
    assert await entry.runtime_data.async_set_mode("1", "Test") == "TMODE"
    assert firmware.mode[0] == "wmbus_t"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_wmbus_c_not_supported_by_a_culfw(hass: HomeAssistant, mods, firmware, caplog):
    """a-culfw hat keinen C-Modus: "brc" wird gar nicht erst gesendet (es würde über die
    globale Variable mbus_mode auch den Empfang anderer Module beenden)."""
    entry = await _setup(hass, mods, roles=("wmbus_c", "rf433", "off", "monitor"))
    assert _cmds(mods["1"]) == ["V", "?"]
    hub = entry.runtime_data
    assert hub.modules["1"].unsupported == "no_cmode"
    assert "keinen C-Modus" in caplog.text
    assert hass.states.get("sensor.maplecun_test_receive_mode_module_1").state == "nicht unterstützt"
    # unaufgefordertes OFF (z. B. Statusabfrage eines anderen Clients): nichts senden
    await mods["1"].send("OFF")
    await _settle(hass)
    assert "brc" not in mods["1"].received
    # Hinweis im Options-Flow
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "modules"})
    assert "Modul 1: Rolle wmbus_c nicht unterstützt" in r["description_placeholders"]["hints"]
    # auch nach dem Neuladen nicht erneut senden (gespeichert)
    await hass.config_entries.async_reload(entry.entry_id)
    await _settle(hass, 0.6)
    assert "brc" not in mods["1"].received
    assert entry.runtime_data.modules["1"].unsupported == "no_cmode"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_wmbus_c_off_reply_other_firmware(hass: HomeAssistant, mods, firmware):
    """Andere Firmware (nicht a-culfw), die auf "brc" mit OFF antwortet: einmal senden, dann markieren."""
    import custom_components.maplecun.hub  # noqa: F401
    orig = firmware.handle
    firmware.handle = lambda inst, cmd: (
        ["V 1.67 CUL868"] if cmd == "V" else orig(inst, cmd))
    entry = await _setup(hass, mods, roles=("wmbus_c", "rf433", "off", "monitor"))
    assert _cmds(mods["1"]) == ["V", "?", "brx", "X21", "brc"]
    assert entry.runtime_data.modules["1"].unsupported == "no_cmode"
    await mods["1"].send("OFF")
    await _settle(hass)
    assert mods["1"].received.count("brc") == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_wmbus_role_without_b_command(hass: HomeAssistant, mods, firmware):
    """Modul 4 (fntab3) kennt kein "b": Rolle nicht unterstützt, nichts senden außer V und ?."""
    entry = await _setup(hass, mods, roles=("off", "rf433", "off", "wmbus_s"))
    assert _cmds(mods["4"]) == ["V", "?"]
    assert entry.runtime_data.modules["4"].unsupported == "no_b"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_wmbus_is_unknown_reply(hass: HomeAssistant, mods, firmware):
    """Antwort "? (brs is unknown) …" -> nicht unterstützt."""
    firmware.cmds[0] = "b C V X"                     # meldet b, kennt brs aber nicht
    orig = firmware.handle
    firmware.handle = lambda inst, cmd: (
        ["? (brs is unknown) Use one of C V X"] if cmd == "brs" else orig(inst, cmd))
    entry = await _setup(hass, mods, roles=("wmbus_s", "rf433", "off", "off"))
    assert entry.runtime_data.modules["1"].unsupported == "unknown"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_wmbus_only_one_module(hass: HomeAssistant, mods, firmware):
    """Zwei wMBus-Rollen (Altkonfiguration): nur das erste Modul wird gesetzt."""
    entry = await _setup(hass, mods, roles=("wmbus_t", "rf433", "wmbus_s", "off"))
    assert "brt" in mods["1"].received and "brs" not in mods["3"].received
    assert firmware.mode == {0: "wmbus_t", 1: "slow", 2: "off", 3: "off"}
    assert hass.states.get("sensor.maplecun_test_receive_mode_module_3").state == "belegt durch Modul 1"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_wmbus_c_unsupported_frees_s_module(hass: HomeAssistant, mods, firmware):
    """Echte Ausgangslage: Modul 1 "C", Modul 3 "S". brc -> OFF, danach bekommt Modul 3 den Empfang."""
    entry = await _setup(hass, mods, roles=("wmbus_c", "rf433", "wmbus_s", "off"))
    await _settle(hass, 0.4)
    assert _cmds(mods["3"]) == ["V", "?", "brx", "X21", "brs"]
    assert "brc" not in mods["1"].received
    assert firmware.receiving() == {2: "wmbus_s"}         # S-Empfang läuft wirklich
    assert hass.states.get("sensor.maplecun_test_receive_mode_module_3").state == "SMODE"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_wmbus_unsolicited_off(hass: HomeAssistant, mods, firmware):
    """Unaufgefordertes OFF -> einmal neu setzen; erneut innerhalb von 5 min -> nicht."""
    entry = await _setup(hass, mods)
    await mods["1"].send("OFF")
    await _settle(hass)
    assert mods["1"].received.count("brt") == 2
    await mods["1"].send("OFF")
    await mods["2"].send("OFF")                    # 433-MHz-Modul: unverändert
    await _settle(hass)
    assert mods["1"].received.count("brt") == 2
    assert _cmds(mods["2"]) == ["V", "?", "X21"]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_wmbus_watchdog(hass: HomeAssistant, mods, firmware, caplog):
    """45 min ohne Zeile -> Modus einmal neu setzen, höchstens einmal pro Stunde."""
    import time
    entry = await _setup(hass, mods)
    hub = entry.runtime_data
    firmware.mode[0] = "off"
    hub.modules["1"].last_line = time.monotonic() - 46 * 60
    await hub._async_watchdog()
    await _settle(hass)
    assert mods["1"].received.count("brt") == 2 and firmware.mode[0] == "wmbus_t"
    assert "seit 46 min keine Zeile" in caplog.text
    hub.modules["1"].last_line = time.monotonic() - 50 * 60
    await hub._async_watchdog()
    await _settle(hass)
    assert mods["1"].received.count("brt") == 2      # innerhalb einer Stunde nicht erneut
    await hass.config_entries.async_unload(entry.entry_id)


async def test_init_mode_off_sends_only_reads(hass: HomeAssistant, mods, firmware):
    """Ohne "Empfangsmodus setzen": nur V und ?, auch der Wächter sendet nichts."""
    import time
    entry = await _setup(hass, mods, init=False)
    assert _cmds(mods["1"]) == ["V", "?"]
    entry.runtime_data.modules["1"].last_line = time.monotonic() - 46 * 60
    await entry.runtime_data._async_watchdog()
    assert _cmds(mods["1"]) == ["V", "?"]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_v1(hass: HomeAssistant, mods):
    entry = MockConfigEntry(domain=DOMAIN, version=1, title="MapleCUN alt", unique_id="127.0.0.1:x",
        data={"host": "127.0.0.1", "port": mods["2"].port},
        options={"it_devices": {"FFFF0F0FFF": {"name": "Lampe", "on": "FF", "off": "F0"}}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    assert entry.version == 2 and entry.unique_id == "127.0.0.1"
    roles = {k: v["role"] for k, v in entry.data["modules"].items()}
    assert list(roles.values()).count("rf433") == 1 and list(roles.values()).count("off") == 3
    assert hass.states.get("switch.lampe").state == "off"
    await hass.config_entries.async_unload(entry.entry_id)


def test_climate_decoder():
    from custom_components.maplecun import climate433 as c
    r = c.decode("s6840BA60DC;  416: 9584")
    assert (r.model, r.key, r.temperature, r.battery_low) == ("Mebus", "mebus_84_1", 18.6, False)
    r = c.decode("s21D2C1A10980ED;  432: 7920")
    assert (r.model, r.temperature, r.channel) == ("NX7674", 18.2, 2)
    assert c.decode("s114735400540E3").temperature == 15.6       # Beispiel aus FHEM
    assert c.decode("s6841BA60DC") is None                          # Prüfsumme falsch
    assert c.decode("omAAAAAAAAAAAAAB50F6") is None


async def test_climate_and_stats(hass: HomeAssistant, mods):
    entry = MockConfigEntry(domain=DOMAIN, version=2, title="MapleCUN test", unique_id="127.0.0.1",
        data={"host": "127.0.0.1", "modules": _modules(mods)},
        options={"init_mode": True, "meters": {}, "revolts": {}, "it_devices": {}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)

    await mods["2"].send("s6840BA60DC;  416: 9584")
    await mods["2"].send("s21D2C1A10980ED;  432: 7920")
    await mods["2"].send("omAAAAAAAAAAAAAB50F6")
    await _settle(hass)
    hub = entry.runtime_data
    assert {"sensor:mebus_84_1", "sensor:nx7674_07_2"} <= set(hub.discovered)
    assert "18.6 °C" in hub.discovered_label("sensor:mebus_84_1")
    assert hub.modules["2"].rx_count == 3                 # Anzeige wird höchstens 1x/min aktualisiert
    assert int(hass.states.get("sensor.maplecun_test_received_today_module_2").state) >= 1
    assert hass.states.get("sensor.maplecun_test_last_received_module_2").state not in ("unknown", "unavailable")

    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "discovered"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"adopt": ["sensor:mebus_84_1"], "ignore_rest": False})
    await _settle(hass, 1.0)
    await mods["2"].send("s8840CB60DB;  416: 9584")
    await _settle(hass)
    ents = [e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) if "thermometer" in e.entity_id]
    print(ents)
    assert float(hass.states.get("sensor.thermometer_mebus_84_1_temperature").state) == 20.3
    assert hass.states.get("binary_sensor.thermometer_mebus_84_1_battery").state == "off"
    assert "sensor:nx7674_07_2" in entry.runtime_data.discovered
    await hass.config_entries.async_unload(entry.entry_id)
