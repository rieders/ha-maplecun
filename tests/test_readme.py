"""Markdown-Karte und Beispiel-Automation aus dem README mit der HA-Template-Engine rendern."""

import re
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("homeassistant")
import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util

README = Path(__file__).parent.parent / "README.md"
EID = "sensor.maplecun_192_168_1_228_empfangene_zahler"


def _blocks():
    md = README.read_text(encoding="utf-8")
    blocks = [yaml.safe_load(b) for b in re.findall(r"```yaml\n(.*?)```", md, re.S)]
    card = next(b for b in blocks if isinstance(b, dict) and b.get("type") == "markdown")
    auto = next(b for b in blocks if isinstance(b, dict) and "triggers" in b)
    return card, auto


def _row(mid, name, hours, rssi=-70.5, relayed=False, count=0):
    ts = (dt_util.now() - timedelta(hours=hours)).isoformat(timespec="seconds") if hours is not None else None
    return {"id": mid, "name": name, "hersteller": "LSE", "typ": "Wasser", "rssi": rssi, "modul": "1",
            "netzwerkknoten": relayed, "letzter_empfang": ts, "telegramme_heute": count}


async def test_readme_card_and_automation(hass: HomeAssistant):
    card, auto = _blocks()
    hass.states.async_set(EID, "3", {
        "eigene": [_row("52144701", "Warmwasser Bad", 3, count=4),
                   _row("16660259", "HKV Bad", 50, rssi=None, relayed=True),
                   _row("11111111", "Zähler 11111111", None, rssi=None)],
        "weitere": [_row("52143906", "", 5 / 60, rssi=-80.0, relayed=True, count=1)],
    })
    out = Template(card["content"], hass).async_render(parse_result=False)
    lines = out.splitlines()
    own = [l for l in lines if l.startswith("| ✅") or l.startswith("| ⚠️")]
    assert len(own) == 3
    assert own[0].startswith("| ✅ | 52144701") and "vor 3 Stunden" in own[0] and "-70.5 dBm" in own[0]
    assert own[1].startswith("| ⚠️ | **16660259**") and "vor 2 Tagen" in own[1] and "| ja |" in own[1]
    assert own[2].startswith("| ⚠️ | **11111111**") and "**nie**" in own[2]
    assert any(l.startswith("| 52143906 | LSE") and "vor 5 Minuten" in l for l in lines)
    # Tabellen bleiben zeilenweise (kein Falten durch YAML)
    assert sum(l.startswith("|---") for l in lines) == 2

    trig = Template(auto["triggers"][0]["value_template"], hass).async_render()
    msg = Template(auto["actions"][0]["data"]["message"], hass).async_render(parse_result=False)
    assert trig is True
    assert "HKV Bad (16660259)" in msg and "Warmwasser Bad" not in msg and "11111111" not in msg

    # alle eigenen Zähler aktuell -> Trigger aus
    hass.states.async_set(EID, "1", {"eigene": [_row("52144701", "Warmwasser Bad", 1)], "weitere": []})
    assert Template(auto["triggers"][0]["value_template"], hass).async_render() is False
