"""Dekoder für Revolt NC-5462 Energiemesser (Aufbau wie FHEM 19_Revolt.pm)."""

from __future__ import annotations

import re

_REVOLT_RE = re.compile(r"^r[0-9a-fA-F]{22}$")

# max. Dauerleistung eines NC-5462 (kW) – für die Sprungprüfung des Energiezählers
MAX_KW = 3.65


def is_revolt(line: str) -> bool:
    """True, wenn die Zeile ein Revolt-Telegramm ist."""
    return bool(_REVOLT_RE.match(line))


def decode(line: str) -> tuple[str, dict[str, float]]:
    """Revolt-Telegramm in (ID, Messwerte) zerlegen."""
    m = line.lower()

    def h(start: int, length: int) -> int:
        return int(m[start : start + length], 16)

    return m[1:5], {
        "voltage": float(h(5, 2)),
        "current": round(h(7, 4) * 0.01, 2),
        "frequency": float(h(11, 2)),
        "power": round(h(13, 4) * 0.1, 1),
        "pf": round(h(17, 2) * 0.01, 2),
        "energy": round(h(19, 4) * 0.01, 2),
    }


def plausible(v: dict[str, float]) -> bool:
    """Plausibilitätsprüfung (wie FHEM / sknorrell.de)."""
    pf = v["pf"] or 0.0001
    if v["voltage"] < 80 or v["frequency"] > 65 or v["power"] > 3650 or v["current"] > 16:
        return False
    if v["current"] == 0 and v["power"] / v["voltage"] / pf > 0.00999:
        return False
    return True
