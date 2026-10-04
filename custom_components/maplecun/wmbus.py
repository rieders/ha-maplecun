"""Wireless M-Bus (EN 13757 / OMS) Dekoder für a-culfw-Telegramme.

Unterstützt:
  * Rahmenformat A (mit Block-CRCs) und B ("bY…")
  * Kurz- (CI 7A) und Langheader (CI 72), ohne Header (CI 78)
  * Extended Link Layer CI 8C
  * Qundis-/Repeater-Rahmen, in denen ein komplettes Zählertelegramm eingepackt ist
  * AES-128-CBC (Sicherheitsmodus 5) mit Schlüssel
  * DIF/VIF-Datensätze: Volumen, Energie, Leistung, Durchfluss, Temperaturen,
    HKV-Einheiten, Datum/Uhrzeit u. a.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import date, datetime

# ---------------------------------------------------------------- Hilfsfunktionen


def crc16(data: bytes) -> int:
    """CRC nach EN 13757-4 (Polynom 0x3D65, invertiert)."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x3D65) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return (~crc) & 0xFFFF


def manufacturer(code: int) -> str:
    """Herstellerkennung (2 Byte) in drei Buchstaben."""
    return "".join(chr(((code >> s) & 0x1F) + 64) for s in (10, 5, 0))


class WMBusError(ValueError):
    """Telegramm nicht auswertbar."""


# ---------------------------------------------------------------- Geräteklassen

DEVICE_TYPES = {
    0x00: "Sonstiges", 0x02: "Strom", 0x03: "Gas", 0x04: "Wärme", 0x06: "Warmwasser",
    0x07: "Wasser", 0x08: "Heizkostenverteiler", 0x0A: "Kälte", 0x0C: "Wärme",
    0x0D: "Wärme/Kälte", 0x15: "Warmwasser", 0x16: "Kaltwasser", 0x1A: "Rauchmelder",
    0x1B: "Raumsensor", 0x31: "Repeater", 0x32: "Repeater", 0x36: "Funkkonverter",
    0x37: "Funkkonverter",
}


# ---------------------------------------------------------------- Rahmen


def _strip_format_a(raw: bytes) -> tuple[bytes, bytes]:
    """Block-CRCs entfernen. Liefert (Telegramm, Rest z. B. RSSI)."""
    length = raw[0]
    total = length + 1                      # Telegrammbytes ohne CRCs
    out = bytearray()
    pos = 0
    first = True
    while len(out) < total:
        n = 10 if first else min(16, total - len(out))
        block = raw[pos : pos + n]
        crc = raw[pos + n : pos + n + 2]
        if len(block) < n or len(crc) < 2:
            raise WMBusError("Telegramm zu kurz")
        if crc16(block) != int.from_bytes(crc, "big"):
            raise WMBusError("CRC-Fehler")
        out += block
        pos += n + 2
        first = False
    return bytes(out), raw[pos:]


def _strip_format_b(raw: bytes) -> tuple[bytes, bytes]:
    """Rahmenformat B: L zählt die CRC-Bytes mit."""
    length = raw[0]
    total = length + 1
    if len(raw) < total:
        raise WMBusError("Telegramm zu kurz")
    if total <= 128:
        body = raw[: total - 2]
        if crc16(body) != int.from_bytes(raw[total - 2 : total], "big"):
            raise WMBusError("CRC-Fehler")
        tele = body
    else:
        b2 = raw[:126]
        if crc16(b2) != int.from_bytes(raw[126:128], "big"):
            raise WMBusError("CRC-Fehler")
        b3 = raw[128 : total - 2]
        if crc16(b3) != int.from_bytes(raw[total - 2 : total], "big"):
            raise WMBusError("CRC-Fehler")
        tele = b2 + b3
    # L auf "ohne CRC" umrechnen, damit der Rest einheitlich bleibt
    tele = bytes([len(tele) - 1]) + tele[1:]
    return tele, raw[total:]


def parse_cul_line(line: str) -> tuple[bytes, int | None]:
    """a-culfw-Zeile "b…" bzw. "bY…" -> (Telegramm ohne CRC, RSSI dBm)."""
    if not line.startswith("b"):
        raise WMBusError("keine Wireless-M-Bus-Zeile")
    hexpart = line[1:].split("::", 1)[0].strip()
    frame_b = hexpart.startswith("Y")
    if frame_b:
        hexpart = hexpart[1:]
    try:
        raw = bytes.fromhex(hexpart)
    except ValueError as err:
        raise WMBusError("ungültige Hex-Daten") from err
    if len(raw) < 12:
        raise WMBusError("Telegramm zu kurz")
    tele, rest = _strip_format_b(raw) if frame_b else _strip_format_a(raw)
    rssi = None
    if len(rest) >= 2:
        # a-culfw (X21) hängt LQI und RSSI an; RSSI -> dBm wie im CC1101-Datenblatt
        r = rest[1]
        rssi = round((r - 256) / 2 - 74 if r >= 128 else r / 2 - 74, 1)
    return tele, rssi


# ---------------------------------------------------------------- Datensätze


@dataclass
class Record:
    """Ein DIF/VIF-Datensatz."""

    quantity: str          # z. B. "volume", "energy", "date"
    unit: str | None
    value: float | int | str | date | datetime | None
    storage: int = 0
    tariff: int = 0
    function: str = "instantaneous"   # instantaneous / max / min / error

    @property
    def key(self) -> str:
        k = self.quantity
        if self.function != "instantaneous":
            k += f"_{self.function}"
        if self.tariff:
            k += f"_t{self.tariff}"
        if self.storage:
            k += f"_s{self.storage}"
        return k


@dataclass
class Telegram:
    """Ausgewertetes Telegramm eines Zählers."""

    manufacturer: str
    meter_id: str
    version: int
    device_type: int
    access_no: int | None = None
    status: int | None = None
    encrypted: bool = False
    records: list[Record] = field(default_factory=list)
    rssi: float | None = None
    via: str | None = None            # ID des Repeaters, falls eingepackt

    @property
    def type_name(self) -> str:
        return DEVICE_TYPES.get(self.device_type, f"Typ {self.device_type}")

    def values(self) -> dict[str, object]:
        """Werte je Schlüssel; der erste Datensatz gewinnt."""
        out: dict[str, object] = {}
        for rec in self.records:
            out.setdefault(rec.key, rec.value)
        return out


_FUNC = {0: "instantaneous", 1: "max", 2: "min", 3: "error"}


def _decode_vif(vif: int, vifes: list[int]) -> tuple[str, str | None, float]:
    """Primären VIF in (Größe, Einheit, Faktor) übersetzen."""
    v = vif & 0x7F
    n = v & 0x07
    if v <= 0x07:
        return "energy", "kWh", 10 ** (n - 3) / 1000
    if v <= 0x0F:
        return "energy_j", "MJ", 10 ** n / 1e6
    if v <= 0x17:
        return "volume", "m³", 10 ** (n - 6)
    if v <= 0x1F:
        return "mass", "kg", 10 ** (n - 3)
    if v <= 0x27:
        return "operating_time" if v >= 0x24 else "on_time", "h", 0
    if v <= 0x2F:
        return "power", "W", 10 ** (n - 3)
    if v <= 0x37:
        return "power_j", "kJ/h", 10 ** n / 1000
    if v <= 0x3F:
        return "volume_flow", "m³/h", 10 ** (n - 6)
    if v <= 0x47:
        return "volume_flow_ext", "m³/h", 10 ** (n - 7) * 60
    if v <= 0x4F:
        return "volume_flow_ext", "m³/h", 10 ** (n - 9) * 3600
    if v <= 0x57:
        return "mass_flow", "kg/h", 10 ** (n - 3)
    if v <= 0x5B:
        return "flow_temperature", "°C", 10 ** ((v & 0x03) - 3)
    if v <= 0x5F:
        return "return_temperature", "°C", 10 ** ((v & 0x03) - 3)
    if v <= 0x63:
        return "temperature_difference", "K", 10 ** ((v & 0x03) - 3)
    if v <= 0x67:
        return "external_temperature", "°C", 10 ** ((v & 0x03) - 3)
    if v <= 0x6B:
        return "pressure", "bar", 10 ** ((v & 0x03) - 3)
    if v == 0x6C:
        return "date", None, 1
    if v == 0x6D:
        return "datetime", None, 1
    if v == 0x6E:
        return "hca", None, 1
    if v <= 0x73:
        return "averaging_duration", None, 0
    if v <= 0x77:
        return "actuality_duration", None, 0
    if v == 0x78:
        return "fabrication_no", None, 1
    if v == 0x7A:
        return "bus_address", None, 1
    return f"vif_{v:02x}", None, 1


def _date_g(b: bytes) -> date | None:
    if b == b"\xff\xff":
        return None
    day = b[0] & 0x1F
    month = b[1] & 0x0F
    year = 2000 + (((b[0] & 0xE0) >> 5) | ((b[1] & 0xF0) >> 1))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _datetime_f(b: bytes) -> datetime | None:
    if b[0] & 0x80:            # "ungültig"-Bit
        return None
    minute = b[0] & 0x3F
    hour = b[1] & 0x1F
    day = b[2] & 0x1F
    month = b[3] & 0x0F
    year = 2000 + (((b[2] & 0xE0) >> 5) | ((b[3] & 0xF0) >> 1))
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _bcd(b: bytes) -> int | None:
    s = b[::-1].hex()
    if s and s[0] == "f":          # negatives Vorzeichen
        s = "-" + s[1:]
    try:
        return int(s)
    except ValueError:
        return None


_DATA_LEN = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 4, 6: 6, 7: 8, 9: 1, 10: 2, 11: 3, 12: 4, 14: 6}


def parse_records(data: bytes) -> list[Record]:
    """DIF/VIF-Datensätze dekodieren (bricht bei Herstellerdaten ab)."""
    records: list[Record] = []
    i = 0
    while i < len(data):
        dif = data[i]
        i += 1
        if dif == 0x2F:                     # Füllbyte
            continue
        if dif in (0x0F, 0x1F):             # herstellerspezifische Daten folgen
            break
        dlen_code = dif & 0x0F
        func = _FUNC[(dif >> 4) & 0x03]
        storage = (dif >> 6) & 0x01
        tariff = 0
        ext = dif & 0x80
        shift_s, shift_t = 1, 0
        while ext:
            if i >= len(data):
                raise WMBusError("Datensatz abgeschnitten")
            dife = data[i]
            i += 1
            storage |= (dife & 0x0F) << shift_s
            tariff |= ((dife >> 4) & 0x03) << shift_t
            shift_s += 4
            shift_t += 2
            ext = dife & 0x80
        if i >= len(data):
            raise WMBusError("VIF fehlt")
        vif = data[i]
        i += 1
        vifes: list[int] = []
        if vif in (0xFD, 0xFB, 0x7C, 0xFC):
            # Erweiterte Tabellen / Klartext-VIF: nur überspringen
            if vif in (0x7C, 0xFC):
                ln = data[i]
                i += 1 + ln
            ext = True
            while ext and i < len(data):
                vifes.append(data[i])
                ext = data[i] & 0x80
                i += 1
            quantity, unit, factor = (f"vife_{vif:02x}_{vifes[0] if vifes else 0:02x}", None, 1)
        else:
            ext = vif & 0x80
            while ext and i < len(data):
                vifes.append(data[i])
                ext = data[i] & 0x80
                i += 1
            quantity, unit, factor = _decode_vif(vif, vifes)
            if vif in (0x7F, 0xFF):
                quantity, unit, factor = "manufacturer", None, 1

        # Datenlänge
        if dlen_code == 0x0D:               # variable Länge
            ln = data[i]
            i += 1
            raw = data[i : i + (ln & 0x3F)]
            i += ln & 0x3F
            value: object = raw.hex()
        else:
            if dlen_code not in _DATA_LEN:
                raise WMBusError(f"unbekannte Datenlänge {dlen_code:x}")
            n = _DATA_LEN[dlen_code]
            raw = data[i : i + n]
            if len(raw) < n:
                raise WMBusError("Daten abgeschnitten")
            i += n
            if quantity == "date" and n == 2:
                value = _date_g(raw)
            elif quantity == "datetime" and n == 4:
                value = _datetime_f(raw)
            elif quantity in ("date", "datetime"):
                value = None
            elif 9 <= dlen_code <= 14:
                num = _bcd(raw)
                value = None if num is None else num
            elif dlen_code == 5:
                value = struct.unpack("<f", raw)[0]
            elif n:
                num = int.from_bytes(raw, "little", signed=False)
                # alle Bits gesetzt = ungültig
                value = None if num == (1 << (8 * n)) - 1 and func == "error" else num
            else:
                value = None
            if isinstance(value, (int, float)) and factor not in (0, 1):
                value = round(value * factor, 6)
        records.append(
            Record(quantity=quantity, unit=unit, value=value,
                   storage=storage, tariff=tariff, function=func)
        )
    return records


# ---------------------------------------------------------------- Telegramm


def _decrypt_mode5(enc: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    usable = len(enc) - len(enc) % 16
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(enc[:usable]) + dec.finalize() + enc[usable:]


def _find_embedded(payload: bytes) -> bytes | None:
    """Eingepacktes Telegramm (Repeater) suchen: L-Feld passt exakt zur Restlänge."""
    for pos in range(len(payload) - 11):
        if payload[pos] == len(payload) - pos - 1 and payload[pos + 1] in (0x44, 0x46, 0x08, 0x06):
            return payload[pos:]
    return None


def decode(tele: bytes, keys: dict[str, bytes] | None = None, _depth: int = 0) -> Telegram:
    """Telegramm (ohne CRCs, beginnend mit L) auswerten."""
    if len(tele) < 11:
        raise WMBusError("Telegramm zu kurz")
    man_raw = tele[2:4]
    a_field = tele[4:10]
    t = Telegram(
        manufacturer=manufacturer(int.from_bytes(man_raw, "little")),
        meter_id=a_field[3::-1].hex(),
        version=a_field[4],
        device_type=a_field[5],
    )
    ci = tele[10]
    pos = 11

    if ci == 0x8C:                          # Extended Link Layer (kurz)
        pos += 2
        ci = tele[pos]
        pos += 1

    if ci == 0x7A:                          # Kurzheader
        acc, st, cfg = tele[pos], tele[pos + 1], tele[pos + 2 : pos + 4]
        pos += 4
        iv_head = man_raw + a_field
    elif ci == 0x72:                        # Langheader
        hid = tele[pos : pos + 4]
        hman = tele[pos + 4 : pos + 6]
        t.meter_id = hid[::-1].hex()
        t.manufacturer = manufacturer(int.from_bytes(hman, "little"))
        t.version, t.device_type = tele[pos + 6], tele[pos + 7]
        acc, st, cfg = tele[pos + 8], tele[pos + 9], tele[pos + 10 : pos + 12]
        pos += 12
        iv_head = hman + hid + bytes([t.version, t.device_type])
    elif ci == 0x78:                        # ohne Header
        acc = st = None
        cfg = b"\x00\x00"
        iv_head = b""
    else:
        inner = _find_embedded(tele[pos:]) if _depth < 2 else None
        if inner is not None:
            sub = decode(inner, keys, _depth + 1)
            sub.via = t.meter_id
            return sub
        raise WMBusError(f"CI {ci:02X} wird nicht unterstützt")

    t.access_no, t.status = acc, st
    mode = cfg[1] & 0x1F
    payload = tele[pos:]
    if mode == 5:
        t.encrypted = True
        key = (keys or {}).get(t.meter_id)
        if key is None:
            return t                         # verschlüsselt, kein Schlüssel
        nblocks = cfg[0] >> 4
        enc_len = nblocks * 16 if nblocks else len(payload) - len(payload) % 16
        iv = iv_head + bytes([acc]) * 8
        payload = _decrypt_mode5(payload[:enc_len], key, iv) + payload[enc_len:]
        if payload[:2] != b"\x2f\x2f":
            raise WMBusError("Entschlüsselung fehlgeschlagen (falscher Schlüssel?)")
    elif mode not in (0,):
        t.encrypted = True
        return t
    t.records = parse_records(payload)
    return t
