"""Batterie-Frühwarnung für Intertechno-Fernbedienungen (ohne Batteriemeldung).

Am Gerät beobachtet (MapleCUN, Modul 2, X21): mit schwacher Batterie kamen nur noch
verstümmelte Pakete an (om-Zeilen, 7 von 20 Tastendrücken), mit neuer Batterie 10 von 10 bei
etwa -62 dBm, nach 7 von 10 Drücken gefolgt von einem abgeschnittenen Paketende.

Daraus zwei Anzeichen je Fernbedienung (= Hauscode, die ersten 5 Tristate-Stellen,
PT2262/Elro: DIP-Schalter "System"):
* Signalstärke: gleitender Median der letzten 20 Tastendrücke gegenüber dem besten bisher
  erreichten Median (Referenz). Schwach, wenn er mindestens <Schwelle> dB darunter liegt –
  ausgewertet erst ab 10 Tastendrücken.
* Anteil der Tastendrücke, nach denen der Phantom-Filter ein verstümmeltes Paketende verworfen
  hat (nur als Attribut, löst nicht allein aus).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from statistics import median

WINDOW = 20          # Tastendrücke im gleitenden Median
MIN_PRESSES = 10     # erst ab so vielen Tastendrücken auswerten
DEFAULT_THRESHOLD_DB = 10


def house_code(code: str) -> str:
    """Hauscode einer Fernbedienung: die ersten 5 von 10 Tristate-Stellen."""
    return code[:5]


def rssi_dbm(raw: int) -> float:
    """RSSI-Byte des CC1101 (von a-culfw mit X21 angehängt) in dBm – wie FHEM 00_CUL.pm."""
    return (raw - 256) / 2 - 74 if raw >= 128 else raw / 2 - 74


@dataclass
class RemoteStats:
    """Verlauf einer Fernbedienung; wird im Speicher der Integration gesichert."""

    rssi: deque = field(default_factory=lambda: deque(maxlen=WINDOW))
    garbled: deque = field(default_factory=lambda: deque(maxlen=WINDOW))
    reference: float | None = None

    @property
    def presses(self) -> int:
        return len(self.rssi)

    @property
    def median(self) -> float | None:
        return round(median(self.rssi), 1) if self.rssi else None

    @property
    def garbled_ratio(self) -> float | None:
        return round(sum(self.garbled) / len(self.garbled), 2) if self.garbled else None

    def add_press(self, dbm: float) -> None:
        self.rssi.append(dbm)
        self.garbled.append(False)
        if self.presses >= MIN_PRESSES and (self.reference is None or self.median > self.reference):
            self.reference = self.median           # bester bisher erreichter Median

    def mark_garbled(self) -> None:
        """Letzter Tastendruck war von einem verstümmelten Paketende gefolgt."""
        if self.garbled:
            self.garbled[-1] = True

    def weak(self, threshold_db: float) -> bool | None:
        """None = noch nicht genug Tastendrücke, sonst ob die Batterie schwach ist."""
        if self.presses < MIN_PRESSES or self.reference is None:
            return None
        return self.median <= self.reference - threshold_db

    def reset(self) -> None:
        """Batterie getauscht: Referenz und Verlauf neu beginnen."""
        self.rssi.clear()
        self.garbled.clear()
        self.reference = None

    def to_dict(self) -> dict:
        return {"rssi": list(self.rssi), "garbled": list(self.garbled), "reference": self.reference}

    @classmethod
    def from_dict(cls, data: dict) -> RemoteStats:
        stats = cls()
        stats.rssi.extend(float(x) for x in data.get("rssi", [])[-WINDOW:])
        garbled = [bool(x) for x in data.get("garbled", [])][-WINDOW:]
        stats.garbled.extend(garbled + [False] * (len(stats.rssi) - len(garbled)))
        ref = data.get("reference")
        stats.reference = float(ref) if ref is not None else None
        return stats
