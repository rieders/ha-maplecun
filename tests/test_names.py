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


# Alles einrichten, was Entitäten erzeugt: Zähler, Revolt, Intertechno, Thermometer
FULL_OPTIONS = {
    "init_mode": True,
    "meters": {"52144701": {"name": "", "key": ""}, "16660259": {"name": "", "key": ""}},
    "revolts": {"3a65": {"name": "Revolt 3a65"}},
    "it_devices": {"FFFF0F0FFF": {"name": "Lampe", "on": "FF", "off": "F0"}},
    "sensors": {"mebus_84_1": {"name": "Thermometer mebus_84_1", "model": "Mebus"}},
}
REVOLT = "r3a65E6006432" "08FC" "64" "04D2"
CLIMATE = "s6840BA60DC;  416: 9584"
# Intertechno-Schalter: einzige Funktion seines eigenen Geräts ("Lampe") – heißt bewusst wie das
# Gerät (HA-Konvention _attr_name = None), nicht wie der MapleCUN
MAIN_FEATURE_DOMAINS = {"switch"}


async def _check_all_names(hass, mods):
    """Jede Entität aller Plattformen (auch standardmäßig deaktivierte) hat einen eigenen Namen."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, title="MapleCUN 192.168.1.228", unique_id="127.0.0.1",
        data={"host": "127.0.0.1", "modules": _modules(mods, ("wmbus_t", "rf433", "monitor", "off"))},
        options=FULL_OPTIONS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.6)
    await mods["1"].send(WARM)
    await mods["1"].send(RELAYED_HCA)
    await mods["2"].send(REVOLT)
    await mods["2"].send(CLIMATE)
    await _settle(hass, 0.6)
    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    # Deaktivierte einschalten, damit auch ihr Name über den Zustand geprüft wird
    for ent in entries:
        if ent.disabled_by:
            ent_reg.async_update_entity(ent.entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await _settle(hass, 0.6)
    entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    domains = {e.domain for e in entries}
    assert {"sensor", "binary_sensor", "button", "number", "select", "switch"} <= domains, domains
    assert len(entries) > 40
    bad = []
    for ent in entries:
        dev = dev_reg.async_get(ent.device_id)
        dev_name = dev.name_by_user or dev.name
        state = hass.states.get(ent.entity_id)
        friendly = state.attributes.get("friendly_name") if state else None
        if ent.domain in MAIN_FEATURE_DOMAINS:
            assert friendly == dev_name
            continue
        if not ent.original_name or not friendly or not friendly.strip() or friendly == dev_name:
            bad.append((ent.entity_id, ent.original_name, friendly))
    await hass.config_entries.async_unload(entry.entry_id)
    return bad


@pytest.mark.parametrize("language", ["de", "en"])
async def test_every_entity_of_every_platform_has_a_name(hass: HomeAssistant, mods, language):
    hass.config.language = language
    assert await _check_all_names(hass, mods) == []


@pytest.mark.parametrize("language", ["de", "en"])
async def test_names_without_translations(hass: HomeAssistant, mods, language, monkeypatch):
    """Fehlt die Übersetzung in der Installation (veraltete/fehlende translations/*.json),
    bekommt jede Entität den englischen Rückfallnamen statt nur den Gerätenamen."""
    from homeassistant.helpers import translation

    orig = translation.async_get_translations

    async def no_entity_translations(hass_, lang, category, integrations=None, *args, **kwargs):
        if category == "entity" and integrations and DOMAIN in integrations:
            return {}
        return await orig(hass_, lang, category, integrations, *args, **kwargs)

    monkeypatch.setattr(translation, "async_get_translations", no_entity_translations)
    hass.config.language = language
    assert await _check_all_names(hass, mods) == []


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
