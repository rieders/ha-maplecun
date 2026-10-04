import asyncio
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maplecun.const import DOMAIN
from custom_components.maplecun import wmbus

REVOLT = "r3a65E6006432" "08FC" "64" "04D2"   # 230V 1.00A 50Hz 230.0W pf1.00 12.34kWh
# echtes Telegramm Warmwasser 52144701 (+ LQI 80, RSSI 07 wie von a-culfw angehängt)
WARM = "b2D446532014714521A06B78D7AE40000000C13297102004C13429900370400426C3F3C02BB560000326CFFFF046D30A30D08443A43758007"
# echtes Telegramm über Qundis-Repeater (Kaltwasser 52143906)
REPEATED = "b4BC465B276908000F13178A7A0831100AB327001394465320639145209761A077ACD0000000C13078303004C13615B7A350100426C3F3C02BB560000326CFFFFB234046D0A095E3982046C5F388C04135254A1DC0300DFF28007"


class FakeModule:
    def __init__(self):
        self.received: list[str] = []
        self.clients: list[asyncio.StreamWriter] = []

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        self.clients.append(writer)
        while line := await reader.readline():
            self.received.append(line.decode().strip())

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

    async def stop(self):
        for w in self.clients:
            w.close()
        self.server.close()


@pytest.fixture
async def mods(socket_enabled):
    m = {k: FakeModule() for k in "1234"}
    for x in m.values():
        await x.start()
    yield m
    for x in m.values():
        await x.stop()


async def _settle(hass, t=0.3):
    for _ in range(int(t / 0.05)):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()


def _modules(mods, roles=("wmbus_c", "rf433", "off", "monitor")):
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
    assert t2.meter_id == "52143906" and t2.via == "00809076"
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
    data.update(role_1="wmbus_c", role_2="rf433", role_3="off", role_4="monitor")
    bad = {**data, "port_4": 1}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], bad)
    assert r["errors"] == {"port_4": "cannot_connect"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], data)
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"]["modules"]["2"]["role"] == "rf433"
    assert "FFFF0F0FFF" in r["options"]["it_devices"]
    await _settle(hass, 0.6)
    assert mods["1"].received[:2] == ["X21", "brc"]
    assert mods["2"].received[:1] == ["X21"]
    assert mods["4"].received == []           # monitor: nichts senden
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ Betrieb
from custom_components.maplecun import intertechno


async def test_full(hass: HomeAssistant, mods):
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

    # Unbekannte Fernbedienung + Zähler -> nur "gefunden", Benachrichtigung
    await mods["2"].send(intertechno.encode("0000FFFF00", "FF") + "20")
    await mods["1"].send(WARM)
    await mods["1"].send(REPEATED)
    await _settle(hass)
    hub = entry.runtime_data
    assert set(hub.discovered) == {"it:0000FFFF00", "meter:52144701", "meter:52143906"}
    notes = hass.data.get("persistent_notification") or {}
    from homeassistant.components import persistent_notification as pn
    assert any("neue Geräte" in (n.get("message") or "") for n in pn._async_get_or_create_notifications(hass).values())
    seen = hass.states.get("sensor.maplecun_test_received_meters")
    assert seen.state == "2"

    # Gefundene Geräte: Warmwasser übernehmen, Rest ignorieren
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["menu_options"][0] == "discovered"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "discovered"})
    labels = r["data_schema"].schema["adopt"].options
    print(labels)
    assert "Warmwasser" in labels["meter:52144701"] and "Repeater 00809076" in labels["meter:52143906"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"adopt": ["meter:52144701"], "ignore_rest": True})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await _settle(hass, 1.0)
    hub = entry.runtime_data
    assert "52144701" in entry.options["meters"]
    assert hub.discovered == {} and {"it:0000FFFF00", "meter:52143906"} <= hub.ignored
    # ignorierte tauchen nicht wieder auf
    await mods["1"].send(REPEATED)
    await _settle(hass)
    assert hub.discovered == {}

    await mods["1"].send(WARM)
    await _settle(hass)
    st = hass.states.get("sensor.warmwasser_52144701_meter_reading")
    assert float(st.state) == 27.129
    assert st.attributes["device_class"] == "water" and st.attributes["state_class"] == "total_increasing"
    assert float(hass.states.get("sensor.warmwasser_52144701_due_date_value").state) == 9.942
    assert hass.states.get("sensor.warmwasser_52144701_due_date").state == "2025-12-31"
    assert float(hass.states.get("sensor.warmwasser_52144701_signal_strength").state) == -70.5

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
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"auto_revolt": True, "auto_it": False, "auto_meter": False, "reset_ignored": True})
    await _settle(hass, 1.0)
    await mods["1"].send(REPEATED)
    await _settle(hass)
    assert "meter:52143906" in entry.runtime_data.discovered

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
