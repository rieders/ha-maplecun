"""Rückfallnamen für alle Plattformen.

Home Assistant nimmt den Namen einer Entität mit has_entity_name aus der Übersetzung
(translations/<sprache>.json, Schlüssel entity.<plattform>.<translation_key>.name). Findet es
dort nichts, fällt es auf entity_description.name zurück – und ohne den auf gar keinen Namen:
die Oberfläche zeigt dann nur den Gerätenamen ("MapleCUN 192.168.1.228"). Damit das nie
passiert, bekommt jede Entität mit translation_key den englischen Namen aus strings.json als
entity_description.name. Die Übersetzung hat weiterhin Vorrang (deutsch bleibt deutsch).
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from homeassistant.helpers.entity import Entity, EntityDescription

_ENTITY_STRINGS: dict[str, dict] = json.loads(
    (Path(__file__).parent / "strings.json").read_text(encoding="utf-8")
)["entity"]


def fallback_name(platform: str, key: str | None, placeholders: dict | None = None) -> str | None:
    """Englischer Name aus strings.json mit eingesetzten Platzhaltern (oder None)."""
    if not key:
        return None
    name = _ENTITY_STRINGS.get(platform, {}).get(key, {}).get("name")
    if not name:
        return None
    try:
        return name.format(**(placeholders or {}))
    except (KeyError, IndexError):
        return name


def with_fallback_name(entity: Entity, platform: str, description_cls: type[EntityDescription]) -> None:
    """entity_description.name setzen, falls die Entität einen translation_key hat."""
    desc = getattr(entity, "entity_description", None)
    key = getattr(entity, "_attr_translation_key", None) or (desc.translation_key if desc else None)
    name = fallback_name(platform, key, getattr(entity, "_attr_translation_placeholders", None))
    if name is None:
        return
    if desc is None:
        desc = description_cls(key=key)
    entity.entity_description = replace(desc, name=name)
