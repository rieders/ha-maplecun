"""Entitätsnamen, Übersetzungen und Bereinigung alter Registry-Einträge."""

import json
from pathlib import Path
import re

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.maplecun.const import DOMAIN

from .test_maplecun import RELAYED_HCA, WARM, _modules, _settle, firmware, mods  # noqa: F401

COMPONENT = Path(__file__).parents[1] / "custom_components" / "maplecun"


def _entry(hass, mods, meters=("52144701", "16660259"), roles=("wmbus_t", "rf433", "monitor", "off")):
    entry = MockConfigEntry(domain=DOMAIN, version=2, title="MapleCUN test", unique_id="127.0.0.1",
        data={"host": "127.0.0.1", "modules": _modules(mods, roles)},
        options={"init_mode": True, "revolts": {}, "it_devices": {},
                 "meters": {m: {"name": "", "key": ""} for m in meters}})
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize("language", ["de", "en"])
async def test_every_entity_has_a_name(hass: HomeAssistant, mods, language):
    hass.config.language = language
    entry = _entry(hass, mods)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    await mods["1"].send(WARM)
    await mods["1"].send(RELAYED_HCA)
    await _settle(hass, 0.6)

    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    assert len(entries) > 20
    nameless = []
    for ent in entries:
        dev = dev_reg.async_get(ent.device_id)
        # Name in der Registry (so zeigt die Oberfläche auch deaktivierte Entitäten an)
        if not ent.original_name:
            nameless.append(ent.entity_id)
            continue
        if ent.disabled_by:
            continue
        friendly = hass.states.get(ent.entity_id).attributes.get("friendly_name")
        if friendly == (dev.name_by_user or dev.name):
            nameless.append(ent.entity_id)
    assert nameless == []
    await hass.config_entries.async_unload(entry.entry_id)


def test_translation_keys_complete():
    """Jeder im Code verwendete translation_key steht in strings.json, en.json und de.json."""
    code = "\n".join(p.read_text(encoding="utf-8") for p in COMPONENT.glob("*.py"))
    used = set(re.findall(r'translation_key\s*=\s*"([a-z0-9_]+)"', code))
    used |= {f"last_raw_{m}" for m in "1234"}          # f"last_raw_{module_id}"
    files = {name: json.loads((COMPONENT / name).read_text(encoding="utf-8"))
             for name in ("strings.json", "translations/en.json", "translations/de.json")}
    for name, data in files.items():
        keys = set(data["entity"]["sensor"]) | set(data["entity"]["binary_sensor"]) | set(
            data["entity"].get("button", {})) | set(data["entity"].get("number", {})) | set(
            data["entity"].get("select", {}))
        missing = {k for k in used if k not in keys and k not in data.get("selector", {})
                   and k not in data.get("exceptions", {}) and k != "role"}
        assert missing == set(), f"{name}: {missing}"

    def leaves(d, p=""):
        out = set()
        for k, v in d.items():
            out |= leaves(v, f"{p}/{k}") if isinstance(v, dict) else {f"{p}/{k}"}
        return out

    ref = leaves(files["strings.json"])
    assert leaves(files["translations/en.json"]) == ref
    assert leaves(files["translations/de.json"]) == ref


async def test_stale_registry_entries_removed(hass: HomeAssistant, mods, caplog):
    """Einträge früherer Versionen (ohne Namen) werden beim Laden entfernt, aktuelle bleiben."""
    entry = _entry(hass, mods, meters=("52144701",))
    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    main = dev_reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)})
    meter = dev_reg.async_get_or_create(config_entry_id=entry.entry_id,
                                        identifiers={(DOMAIN, f"{entry.entry_id}_meter_52144701")})
    e = entry.entry_id
    old = {
        f"{e}_last_raw": main,                      # 0.1
        f"{e}_last_rx": main,                       # Zeitstempel ohne Modulnummer
        f"{e}_last_raw_4": main,                    # abgeschaltetes Modul
        f"{e}_meter_52144701_last_seen": meter,     # alter Schlüssel
        f"{e}_meter_52144701_volume_t1": meter,     # Tarif – wird nicht (mehr) angelegt
        f"{e}_meter_99999999_volume": meter,        # entfernter Zähler
    }
    for uid, dev in old.items():
        ent_reg.async_get_or_create("sensor", DOMAIN, uid, config_entry=entry, device_id=dev.id,
                                    has_entity_name=True, original_name=None)
    keep = ent_reg.async_get_or_create("sensor", DOMAIN, f"{e}_last_raw_1", config_entry=entry,
                                       device_id=main.id, has_entity_name=True, original_name=None,
                                       disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    uids = {x.unique_id for x in er.async_entries_for_config_entry(ent_reg, entry.entry_id)}
    assert not (uids & set(old))
    assert "Verwaiste Sensor-Einträge entfernt" in caplog.text
    # Aktueller, deaktivierter Eintrag bleibt und bekommt beim Laden seinen Namen
    kept = ent_reg.async_get(keep.entity_id)
    assert kept is not None and kept.original_name == "Raw data module 1"
    assert kept.translation_key == "last_raw_1"
    await hass.config_entries.async_unload(entry.entry_id)
