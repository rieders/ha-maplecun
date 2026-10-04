import asyncio
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maplecun.const import DOMAIN

REVOLT = "r3a65E6006432" "08FC" "64" "04D2"   # 230V 1.00A 50Hz 230.0W pf1.00 12.34kWh


class FakeCul:
    def __init__(self):
        self.received: list[str] = []
        self.clients: list[asyncio.StreamWriter] = []

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        self.clients.append(writer)
        while line := await reader.readline():
            cmd = line.decode().strip()
            self.received.append(cmd)
            if cmd == "V":
                writer.write(b"V 1.26.08 a-culfw Build: 323 MapleCUNx4_8F CUL\r\n")
                await writer.drain()

    async def send(self, line: str):
        for w in self.clients:
            w.write(line.encode() + b"\r\n")
            await w.drain()

    async def stop(self):
        for w in self.clients:
            w.close()
        self.server.close()


@pytest.fixture
async def cul(socket_enabled):
    c = FakeCul()
    await c.start()
    yield c
    await c.stop()


async def _settle(hass, t=0.3):
    for _ in range(int(t / 0.05)):
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()


async def test_config_flow(hass: HomeAssistant, cul):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    # ungültiger Code
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": cul.port, "it_codes": "FFFF0F0FFF, XYZ"})
    assert result["errors"] == {"it_codes": "invalid_code"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": cul.port, "it_codes": "ffff0f0fff, FFFF00FFFF"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert set(result["options"]["it_devices"]) == {"FFFF0F0FFF", "FFFF00FFFF"}
    await _settle(hass)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_cannot_connect(hass: HomeAssistant, socket_enabled):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": 1, "it_codes": ""})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_revolt_and_switch(hass: HomeAssistant, cul):
    entry = MockConfigEntry(domain=DOMAIN, title="MapleCUN test",
        data={"host": "127.0.0.1", "port": cul.port},
        options={"it_devices": {"FFFF0F0FFF": {"name": "Steckdose Flur", "on": "FF", "off": "F0"}}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)

    # Schalter
    state = hass.states.get("switch.steckdose_flur")
    assert state is not None and state.state == "off", state
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.steckdose_flur"}, blocking=True)
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.steckdose_flur"}, blocking=True)
    await _settle(hass)
    assert "isFFFF0F0FFFFF" in cul.received and "isFFFF0F0FFFF0" in cul.received
    assert hass.states.get("switch.steckdose_flur").state == "off"

    # Revolt neu entdeckt -> Entitäten mit erstem Wert
    await cul.send(REVOLT)
    await _settle(hass)
    ents = {e.entity_id: e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    print(sorted(ents))
    power = next(i for i in ents if i.endswith("leistung") or i.endswith("_power"))
    energy = next(i for i in ents if i.endswith("energie") or i.endswith("_energy"))
    assert float(hass.states.get(power).state) == 230.0
    assert float(hass.states.get(energy).state) == 12.34
    assert hass.states.get(energy).attributes["state_class"] == "total_increasing"

    # Update + Energiesprung wird verworfen
    await cul.send("r3a65E6006432" "0960" "64" "04D3")   # 240W, 12.35
    await _settle(hass)
    assert float(hass.states.get(power).state) == 240.0
    assert float(hass.states.get(energy).state) == 12.35
    await cul.send("r3a65E6006432" "0960" "64" "FFFF")   # Sprung
    await _settle(hass)
    assert float(hass.states.get(energy).state) == 12.35
    # unplausibel (Spannung 10V) -> ignoriert
    await cul.send("r3a650A006432" "0001" "64" "04D3")
    await _settle(hass)
    assert float(hass.states.get(power).state) == 240.0

    # Fremde Zeile -> Raw (deaktiviert per Default, also nur kein Fehler)
    await cul.send("s8840CB60DB;  416: 9584")
    await _settle(hass)

    # Reload: bekannter Revolt bleibt erhalten
    await hass.config_entries.async_reload(entry.entry_id)
    await _settle(hass, 1.2)
    assert hass.states.get(power) is not None
    devs = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    print([d.name for d in devs])
    assert {"Revolt 3a65", "Steckdose Flur", "MapleCUN test"} <= {d.name for d in devs}

    # Verbindung weg -> nicht verfügbar
    await cul.stop()
    await _settle(hass, 0.5)
    assert hass.states.get("switch.steckdose_flur").state == "unavailable"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_flow(hass: HomeAssistant, cul):
    entry = MockConfigEntry(domain=DOMAIN, title="MapleCUN test",
        data={"host": "127.0.0.1", "port": cul.port}, options={"it_devices": {}})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)

    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] is FlowResultType.MENU and r["menu_options"] == ["add_it"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "add_it"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"name": "Lampe", "code": "ffff0fffff", "on": "ff", "off": "f0"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await _settle(hass, 0.6)
    assert hass.states.get("switch.lampe") is not None

    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "edit_it"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"code": "FFFF0FFFFF"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"on": "F0", "off": "FF"})
    assert entry.options["it_devices"]["FFFF0FFFFF"]["on"] == "F0"
    await _settle(hass, 0.6)

    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "remove_it"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"codes": ["FFFF0FFFFF"]})
    await _settle(hass, 0.6)
    assert entry.options["it_devices"] == {}
    assert hass.states.get("switch.lampe") is None or hass.states.get("switch.lampe").state == "unavailable"
    names = {d.name for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)}
    assert "Lampe" not in names
    await hass.config_entries.async_unload(entry.entry_id)
