"""Intertechno (Tristate, "V1") – Empfang von Fernbedienungen/Wandschaltern.

a-culfw meldet empfangene Codes als "i" + 6 Hex-Zeichen (24 Bit = 12 Tristate-Zeichen),
mit X21 zusätzlich + 2 Hex-Zeichen RSSI. Aufbau wie FHEM 10_IT.pm:
  Bitpaar 00 -> "0", 11 -> "1", 01 -> "F", 10 -> "D"
Die ersten 10 Zeichen sind der Gerätecode, die letzten 2 der Befehl (z. B. FF = Ein, F0 = Aus).
"""

from __future__ import annotations

import re

_IT_RE = re.compile(r"^i([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$")
_PAIR = {"00": "0", "11": "1", "01": "F", "10": "D"}


def decode(line: str) -> tuple[str, str] | None:
    """'i…' -> (Code, Befehl) oder None, wenn es kein Tristate-Telegramm ist."""
    m = _IT_RE.match(line)
    if not m:
        return None
    bits = f"{int(m.group(1), 16):024b}"
    tri = "".join(_PAIR[bits[i : i + 2]] for i in range(0, 24, 2))
    return tri[:10], tri[10:]


def encode(code: str, cmd: str) -> str:
    """Gegenstück zu decode() (für Tests)."""
    rev = {v: k for k, v in _PAIR.items()}
    return f"i{int(''.join(rev[c] for c in code + cmd), 2):06X}"
