"""Entitätsnamen für alle Plattformen – unabhängig vom Übersetzungs-Cache von Home Assistant.

Bis 0.9.1 kam der Name einer Entität mit has_entity_name aus der Übersetzung von Home
Assistant (entity.<plattform>.<translation_key>.name), mit entity_description.name als
Rückfall. In der echten Installation (HA 2026.11, Update über HACS) zeigten number, select
und ein Teil der buttons trotzdem nur den Gerätenamen ("MapleCUN 192.168.1.228"), obwohl
dieselben Dateien im Test überall Namen liefern. Woran es dort hängt (Übersetzungs-Cache nach
dem Update ohne Neustart, alte Registry-Einträge), lässt sich von außen nicht sicher sagen.

Deshalb setzt die Integration den Namen jetzt selbst als _attr_name: aus der eigenen Datei
translations/<sprache>.json (Sprache von Home Assistant, Rückfall englisch), Platzhalter wie
{module} eingesetzt. _attr_name hat in Entity._name_internal Vorrang vor allem anderen und wird
bei jedem Laden als original_name in die Registry geschrieben – auch für deaktivierte Entitäten.
"""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
import json
from pathlib import Path

from homeassistant.helpers.entity import Entity, EntityDescription

_DIR = Path(__file__).parent
_language = "en"


def set_language(language: str | None) -> None:
    """Sprache von Home Assistant (hass.config.language), beim Einrichten gesetzt."""
    global _language
    _language = (language or "en").replace("_", "-")


@lru_cache(maxsize=8)
def _entity_strings(language: str) -> dict:
    path = _DIR / "translations" / f"{language}.json"
    if not path.is_file():
        path = _DIR / "strings.json" if language == "en" else None
        if path is None:
            return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("entity", {})
    except (OSError, ValueError):
        return {}


def entity_name(platform: str, key: str | None, placeholders: dict | None = None,
                language: str | None = None) -> str | None:
    """Name aus translations/<sprache>.json (de-CH -> de -> en), Platzhalter eingesetzt."""
    if not key:
        return None
    lang = language or _language
    for candidate in dict.fromkeys((lang, lang.split("-")[0], "en")):
        name = _entity_strings(candidate).get(platform, {}).get(key, {}).get("name")
        if name:
            try:
                return name.format(**(placeholders or {}))
            except (KeyError, IndexError):
                return name
    return None


def fallback_name(platform: str, key: str | None, placeholders: dict | None = None) -> str | None:
    """Englischer Name (wie in strings.json)."""
    return entity_name(platform, key, placeholders, "en")


_OBJECT_ID_CLASSES: dict[type, type] = {}


def _english_object_id(entity: Entity, english: str) -> None:
    """Entitäts-ID weiterhin aus dem englischen Namen ableiten (HA-Konvention), auch wenn
    _attr_name deutsch ist: suggested_object_id würde sonst _attr_name verwenden."""
    cls = type(entity)
    sub = _OBJECT_ID_CLASSES.get(cls)
    if sub is None:
        sub = type(cls.__name__, (cls,), {
            "__module__": cls.__module__,
            "__qualname__": cls.__qualname__,
            "suggested_object_id": property(lambda self: self._maplecun_object_name),
        })
        _OBJECT_ID_CLASSES[cls] = sub
    entity.__class__ = sub
    entity._maplecun_object_name = english


def with_fallback_name(entity: Entity, platform: str, description_cls: type[EntityDescription]) -> None:
    """Namen setzen, falls die Entität einen translation_key hat.

    _attr_name = Name in der Sprache von Home Assistant; entity_description.name = englisch.
    """
    desc = getattr(entity, "entity_description", None)
    key = getattr(entity, "_attr_translation_key", None) or (desc.translation_key if desc else None)
    placeholders = getattr(entity, "_attr_translation_placeholders", None)
    name = entity_name(platform, key, placeholders)
    if name is None:
        return
    entity._attr_name = name
    english = fallback_name(platform, key, placeholders) or name
    if desc is None:
        desc = description_cls(key=key)
    entity.entity_description = replace(desc, name=english)
    _english_object_id(entity, english)
