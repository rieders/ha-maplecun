"""433-MHz-Temperatur-/Feuchtesensoren aus a-culfw-"s…"-Telegrammen.

Portiert aus FHEM 14_CUL_TCM97001.pm (nur Protokolle mit Prüfsumme, damit keine
Phantom-Sensoren entstehen):
  * Mebus (u. a. Hama, Mebus 40180)            – 10 Hex-Zeichen
  * GT-WT-02 / "Type1" (Temperatur + Feuchte)   – 12 Hex-Zeichen
  * KW9010 (Tchibo, TFA)                        – 12 Hex-Zeichen
  * NX7674 (Rosenstein & Söhne Kühlschrank)     – 14 Hex-Zeichen
Das letzte Byte ist (bei X21) die Signalstärke und wird mitgezählt – wie in FHEM.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ClimateReading:
    model: str
    sensor_id: str            # eindeutig je Sensor (Modell + Kennung + Kanal)
    temperature: float
    humidity: int | None = None
    battery_low: bool | None = None
    channel: int | None = None
    rssi: float | None = None

    @property
    def key(self) -> str:
        return f"{self.model.lower()}_{self.sensor_id}"


def _rssi(byte_hex: str) -> float | None:
    r = int(byte_hex, 16)
    if r == 0:
        return None
    return round((r - 256) / 2 - 74 if r >= 128 else r / 2 - 74, 1)


def _temp10(a: list[int]) -> float:
    """12-Bit-Temperatur aus a[3..5] (Zehntel °C, Zweierkomplement wie in FHEM)."""
    t = ((a[3] << 8) | (a[4] << 4) | a[5]) & 0x3FF
    if a[3] & 0xC == 0xC:
        t = -((~t & 0x3FF) + 1)
    return t / 10


def _plausible(temp: float, hum: int | None = None) -> bool:
    return -40 <= temp <= 70 and (hum is None or 1 <= hum <= 100)


def _mebus(m: str) -> ClimateReading | None:
    a = [int(c, 16) for c in m]
    if (sum(a[1:7]) - 1) & 15 != a[0]:
        return None
    temp = _temp10(a)
    if not _plausible(temp):
        return None
    channel = (a[6] & 0xC) >> 2
    return ClimateReading("Mebus", f"{m[1:3].lower()}_{channel}", temp,
                          battery_low=not bool((a[6] & 0x2) >> 1), channel=channel, rssi=_rssi(m[8:10]))


def _gtwt02(m: str) -> ClimateReading | None:
    a = [int(c, 16) for c in m]
    check = (int(m[7:10], 16) & 0x1F8) >> 3
    gt = (sum(a[0:7]) + (a[7] & 0xE)) % 64 == check
    t1 = sum(a[0:8]) == check
    if not (gt or t1):
        return None
    temp = _temp10(a)
    hum = (int(m[6:8], 16) & 0xFE) >> 1
    if not _plausible(temp, hum):
        return None
    channel = (a[2] & 0x3) + 1
    return ClimateReading("GT_WT_02" if gt else "Type1", f"{m[0:2].lower()}_{channel}", temp, hum,
                          battery_low=(a[2] & 0x8) == 0x8, channel=channel, rssi=_rssi(m[10:12]))


def _kw9010(m: str) -> ClimateReading | None:
    rev = [int(f"{int(c, 16):04b}"[::-1], 2) for c in m]
    if sum(rev[0:8]) & 15 != rev[8]:
        return None
    t = rev[3] + rev[4] * 16 + rev[5] * 256
    temp = -((~t & 0x3FF) + 1) / 10 if rev[5] > 3 else t / 10
    hum = (rev[7] * 16 + rev[6]) - 156
    channel = (int(m[1], 16) & 0xC) >> 2
    if not _plausible(temp, hum) or channel == 0:
        return None
    a2 = int(m[2], 16)
    return ClimateReading("KW9010", f"{m[0:2].lower()}_{channel}", temp, hum,
                          battery_low=not bool(~((a2 & 0x8) >> 3) & 1), channel=channel, rssi=_rssi(m[10:12]))


def _nx7674(m: str) -> ClimateReading | None:
    bits = f"{int(m, 16):0{len(m) * 4}b}"
    crc = 0
    for i in range(34):
        if int(bits[i]) == (crc & 1):
            crc >>= 1
        else:
            crc = (crc >> 1) ^ 12
    crc ^= int(bits[34:38][::-1], 2)
    if crc != int(bits[38:42][::-1], 2):
        return None
    temp = round((int(bits[22:26] + bits[18:22] + bits[14:18], 2) - 1220) * 5 / 90, 1)
    if not _plausible(temp):
        return None
    sensor = 1 if bits[2] == "0" else 2
    rid = int(bits[3:10], 2)
    return ClimateReading("NX7674", f"{rid:02x}_{sensor}", temp,
                          battery_low=bits[35] == "1", channel=sensor, rssi=_rssi(m[12:14]))


def decode(line: str) -> ClimateReading | None:
    """'s…[;timing]' -> Messwert oder None."""
    if not line.startswith("s"):
        return None
    msg = line[1:].split(";", 1)[0].strip()
    try:
        int(msg, 16)
    except ValueError:
        return None
    n = len(msg)
    if n == 10:
        return _mebus(msg)
    if n == 12:
        return _gtwt02(msg) or _kw9010(msg)
    if n == 14:
        return _nx7674(msg)
    return None
