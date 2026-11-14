# SPDX-License-Identifier: GPL-2.0-only
"""Wireless M-Bus (EN 13757 / OMS) Dekoder für a-culfw-Telegramme.

Teile dieses Moduls sind eine Portierung von FHEM ``FHEM/WMBus.pm`` (Autor: kaihs,
FHEM-Forum, SVN-Revision 30375, GPL v2): Extended Link Layer, AFL, Sicherheits-
modus 5 und 7, Rahmenformat B, langer Header, DIF/VIF-Auswertung und die VIF-
Tabellen (siehe wmbus_tables.py). Wo WMBus.pm von EN 13757 abweicht, folgt der
Port der Norm; die Stellen sind mit "Abweichung von WMBus.pm" markiert.

Unterstützt:
  * Rahmenformat A (mit Block-CRCs) und B ("bY…", auch über zwei CRC-Blöcke)
  * Kurz- (CI 7A) und Langheader (CI 72), ohne Header (CI 78)
  * Extended Link Layer CI 8C, 8E, 8D und 8F (AES-128-CTR, Payload-CRC)
  * Authentication and Fragmentation Layer CI 90 (ohne Fragmentierung)
  * AES-128-CBC Sicherheitsmodus 5 und 7 (Schlüsselableitung per AES-CMAC,
    MAC-Prüfung) mit Schlüssel je Zähler
  * Über einen Netzwerkknoten weitergeleitete Telegramme (C=C4, CI=A0, z. B. Qundis-
    Gateways mit "LSE"-Kennung + Bit 15): das innere Zählertelegramm ab Offset 18
  * Sonstige Repeater-Rahmen, in denen ein komplettes Zählertelegramm eingepackt ist
  * DIF/VIF-Datensätze inkl. FD/FB-Erweiterungstabellen, kombinierbarer VIFEs,
    Klartext-VIF und herstellerspezifischer Tabellen (Easymeter, Kamstrup)
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from datetime import date, datetime

try:
    from . import wmbus_tables as tables
except ImportError:  # direkt geladen (Decoder-Tests ohne Home Assistant)
    import wmbus_tables as tables  # type: ignore[no-redef]

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

    def __init__(self, message: str, meter_id: str | None = None) -> None:
        super().__init__(message)
        self.meter_id = meter_id         # gesetzt, wenn der Zähler schon bekannt war


# ---------------------------------------------------------------- Geräteklassen

DEVICE_TYPES = {
    0x00: "Sonstiges", 0x02: "Strom", 0x03: "Gas", 0x04: "Wärme", 0x06: "Warmwasser",
    0x07: "Wasser", 0x08: "Heizkostenverteiler", 0x0A: "Kälte", 0x0C: "Wärme",
    0x0D: "Wärme/Kälte", 0x15: "Warmwasser", 0x16: "Kaltwasser", 0x1A: "Rauchmelder",
    0x1B: "Raumsensor", 0x31: "Repeater", 0x32: "Repeater", 0x36: "Funkkonverter",
    0x37: "Funkkonverter",
}

# Speichernummer der Monatsendwerte je Geräteklasse (Stichtag = Speicher 1)
MONTH_END_STORAGE = {0x06: 8, 0x07: 8, 0x15: 8, 0x16: 8, 0x08: 17}


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
    """Rahmenformat B: L zählt die CRC-Bytes mit; Block 1 höchstens 128 Byte inkl. CRC."""
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
    frame, _lqi, rssi = parse_cul_line_status(line)
    return frame, rssi


def parse_cul_line_status(line: str) -> tuple[bytes, int | None, float | None]:
    """a-culfw-Zeile -> (Telegramm ohne CRC, LQI, RSSI dBm).

    Reihenfolge der angehängten Bytes laut a-culfw clib/rf_mbus.c: DH2(lqi); DH2(rssi);
    am echten MapleCUN z. B. "…802A": LQI 0x80, RSSI 0x2A = -53 dBm.
    """
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
    lqi, rssi = cul_status(rest)
    return tele, lqi, rssi


def cul_status(rest: bytes) -> tuple[int | None, float | None]:
    """Bytes hinter dem Telegramm (Länge aus L-Feld und Block-CRCs) -> (LQI, RSSI dBm).

    a-culfw hängt mit X21 zwei Bytes an: LQI, dann RSSI. Im FHEM-Log fehlt das
    RSSI-Byte (00_CUL.pm schneidet es ab und hängt "::-64" an), dort bleibt nur LQI.
    """
    if len(rest) > 2:
        raise WMBusError(f"{len(rest)} unerwartete Bytes hinter dem Telegramm")
    lqi = rest[0] if rest else None
    if len(rest) < 2:
        return lqi, None
    r = rest[1]
    return lqi, round((r - 256) / 2 - 74 if r >= 128 else r / 2 - 74, 1)


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
    subunit: int = 0
    vif_type: str = ""                # Name aus WMBus.pm, z. B. "VIF_VOLUME"
    extensions: tuple[str, ...] = ()  # kombinierbare VIFEs, Namen aus WMBus.pm

    @property
    def key(self) -> str:
        k = self.quantity
        if self.function != "instantaneous":
            k += f"_{self.function}"
        if self.tariff:
            k += f"_t{self.tariff}"
        if self.subunit:
            k += f"_u{self.subunit}"
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
    encrypted: bool = False           # Nutzdaten waren verschlüsselt
    decrypted: bool = False           # … und wurden mit dem Schlüssel entschlüsselt
    records: list[Record] = field(default_factory=list)
    rssi: float | None = None
    via: str | None = None            # ID des Repeaters, falls eingepackt
    relayed: bool = False             # über Netzwerkknoten (CI A0) empfangen
    security_mode: int | None = None  # 0 unverschlüsselt, 5, 7 …
    mac_valid: bool | None = None     # AFL-MAC (Modus 7) geprüft?
    inner: bytes | None = None        # eingepacktes Zählertelegramm (Netzwerkknoten/Repeater)

    @property
    def type_name(self) -> str:
        return DEVICE_TYPES.get(self.device_type, f"Typ {self.device_type}")

    @property
    def month_storage(self) -> int | None:
        """Speichernummer der Monatsendwerte (8 Wasser, 17 HKV)."""
        return MONTH_END_STORAGE.get(self.device_type)

    def values(self) -> dict[str, object]:
        """Werte je Schlüssel; der erste Datensatz gewinnt."""
        out: dict[str, object] = {}
        for rec in self.records:
            out.setdefault(rec.key, rec.value)
        return out


_FUNC = {0: "instantaneous", 1: "max", 2: "min", 3: "error"}

# Größen, auf denen Sensoren aufbauen: Name in WMBus.pm -> (Schlüssel, Einheit, Zusatzfaktor)
_QUANTITY: dict[str, tuple[str, str | None, float]] = {
    "VIF_ENERGY_WATT": ("energy", "kWh", 1e-3),          # Wh -> kWh
    "VIF_ENERGY": ("energy", "kWh", 1e3),                # FB: MWh -> kWh
    "VIF_VOLUME": ("volume", None, 1),
    "VIF_ELECTRIC_POWER": ("power", None, 1),
    "VIF_VOLUME_FLOW": ("volume_flow", None, 1),
    "VIF_FLOW_TEMP": ("flow_temperature", None, 1),
    "VIF_RETURN_TEMP": ("return_temperature", None, 1),
    "VIF_EXTERNAL_TEMP": ("external_temperature", None, 1),
    "VIF_TEMP_DIFF": ("temperature_difference", None, 1),
    "VIF_TIME_POINT_DATE": ("date", None, 1),
    "VIF_TIME_POINT_DATE_TIME": ("datetime", None, 1),
    "VIF_HCA": ("hca", None, 1),
}
_TIMESPEC = {0: "s", 1: "min", 2: "h", 3: "d"}


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


def _datetime_i(b: bytes) -> datetime | None:
    """Datum/Zeit Typ I (6 Byte, mit Sekunden) – nach EN 13757-3, nicht in WMBus.pm."""
    if b[1] & 0x80:            # "ungültig"-Bit
        return None
    second = b[0] & 0x3F
    minute = b[1] & 0x3F
    hour = b[2] & 0x1F
    day = b[3] & 0x1F
    month = b[4] & 0x0F
    year = 2000 + (((b[3] & 0xE0) >> 5) | ((b[4] & 0xF0) >> 1))
    try:
        return datetime(year, month, day, hour, minute, second)
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


# Länge der Daten je DIF-Datenfeld; 8 = "Selection for readout" ohne Daten
_DATA_LEN = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 4, 6: 6, 7: 8, 8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 14: 6}
_MAX_EXT = 10                       # wie WMBus.pm: höchstens 10 DIFE/VIFE


@dataclass
class _Vib:
    """Ausgewerteter Value Information Block."""

    quantity: str
    unit: str | None
    factor: float
    calc: str | None
    vif_type: str
    exponent: int = 0
    extensions: list[str] = field(default_factory=list)
    ext_codes: list[int] = field(default_factory=list)


def _parse_vib(data: bytes, i: int, mfr: str) -> tuple[_Vib, int]:
    """VIF + VIFEs lesen (EN 13757-3, Tabellen aus WMBus.pm)."""
    vif = data[i]
    i += 1
    code = vif & 0x7F
    ext = vif & 0x80
    vife_table = tables.VIFINFO_OTHER
    entry: tables.VifInfo | None = None
    vib: _Vib | None = None

    if code in (0x7D, 0x7B):                       # Erweiterungstabellen FD / FB
        table = tables.VIFINFO_FD if code == 0x7D else tables.VIFINFO_FB
        code = data[i]
        i += 1
        ext = code & 0x80
        code &= 0x7F
        entry = tables.lookup(table, code)
        if entry is None:
            vib = _Vib(f"vife_{vif:02x}_{code:02x}", None, 1, None, "unknown")
    elif code == 0x7C:                             # Klartext-VIF: Länge + ASCII (rückwärts)
        ln = data[i]
        text = data[i + 1 : i + 1 + ln][::-1].decode("ascii", "replace")
        i += 1 + ln
        vib = _Vib("plaintext", text, 1, None, "see unit")
    elif code == 0x7F:                             # herstellerspezifisch
        if ext and mfr in ("ESY", "KAM"):
            # Easymeter / Kamstrup: nächstes Byte ist ein VIF der Herstellertabelle
            code = data[i]
            i += 1
            ext = code & 0x80
            code &= 0x7F
            entry = tables.lookup(tables.VIFINFO_ESY if mfr == "ESY" else tables.VIFINFO_KAM, code)
            vife_table = tables.VIFINFO_ESY2 if mfr == "ESY" else tables.VIFINFO_KAM2
        else:
            vib = _Vib("manufacturer_specific", None, 1, None, "MANUFACTURER SPECIFIC")
    else:
        if mfr in ("ESY", "KAM"):
            vife_table = tables.VIFINFO_ESY2 if mfr == "ESY" else tables.VIFINFO_KAM2
        entry = tables.lookup(tables.VIFINFO, code)
        if entry is None:                          # z. B. 0x7A Busadresse, 0x7E "beliebiger VIF"
            vib = _Vib(f"vif_{code:02x}", None, 1, None, "unknown")

    if vib is None:
        assert entry is not None
        quantity, unit, scale = _QUANTITY.get(
            entry.name, (entry.name.removeprefix("VIF_").lower(), None, 1)
        )
        unit = unit or entry.unit or None
        if entry.calc == "valueCalcTimeperiod":
            unit = _TIMESPEC[code & entry.exp_mask]
        vib = _Vib(quantity, unit, tables.factor(entry, code) * scale, entry.calc, entry.name,
                   exponent=code & entry.exp_mask)

    # kombinierbare VIFEs (Tabelle "other" bzw. Herstellertabelle)
    count = 0
    while ext:
        count += 1
        if count > _MAX_EXT:
            raise WMBusError("zu viele VIFE")
        vife = data[i]
        i += 1
        ext = vife & 0x80
        e = tables.lookup(vife_table, vife)
        vib.extensions.append(e.name if e else f"VIFE_{vife:02X}")
        vib.ext_codes.append(vife)
        if e and e.name == "VIF_MULTIPLICATIVE_CORRECTION_FACTOR_1000":
            vib.factor *= 1000
        elif e and e.name == "VIF_MULTIPLICATIVE_CORRECTION_FACTOR":
            # Abweichung von WMBus.pm: dort ohne Wirkung; Norm: Faktor 10^(nnn-6)
            vib.factor *= tables.factor(e, vife)
    if vib.ext_codes:
        # Wert mit Erweiterung (z. B. 6D+39) nicht mit dem Grundwert verwechseln
        vib.quantity = f"{vib.quantity}_x{bytes(vib.ext_codes).hex()}"
    return vib, i


def _decimals(factor: float) -> int:
    return max(0, -math.floor(math.log10(factor) + 1e-9)) if factor > 0 else 0


def _scale(value: int | float, factor: float) -> int | float:
    """Wert * Faktor; ganzzahlige Faktoren lassen Ganzzahlen ganzzahlig."""
    if isinstance(value, int) and factor >= 1 and float(factor).is_integer():
        return value * int(factor)
    return round(value * factor, _decimals(factor))


def _ascii(raw: bytes) -> str:
    return "".join(chr(c) if 0x20 <= c < 0x7F else "?" for c in raw)


def parse_records(data: bytes, mfr: str = "") -> list[Record]:
    """DIF/VIF-Datensätze dekodieren.

    Herstellerdaten (DIF 0x0F/0x1F …) werden als ein Datensatz "manufacturer_data"
    (Hex) abgelegt; danach endet die Auswertung. Ein am Ende abgeschnittener
    Datensatz (z. B. Füllbytes weitergeleiteter Telegramme) wird verworfen, die bis
    dahin gelesenen bleiben gültig.
    """
    records: list[Record] = []
    try:
        _parse_records(data, records, mfr)
    except (WMBusError, IndexError, struct.error):
        pass
    return records


def _parse_records(data: bytes, records: list[Record], mfr: str) -> None:
    i = 0
    while i < len(data):
        dif = data[i]
        i += 1
        if dif == 0x2F:                     # Füllbyte
            continue
        dlen_code = dif & 0x0F
        if dlen_code == 0x0F:               # herstellerspezifische Daten / Sonderfunktionen
            if i < len(data):
                records.append(Record("manufacturer_data", None, data[i:].hex(),
                                      vif_type="MANUFACTURER SPECIFIC"))
            return
        func = _FUNC[(dif >> 4) & 0x03]
        storage = (dif >> 6) & 0x01
        tariff = subunit = 0
        ext = dif & 0x80
        n = 0
        while ext:
            n += 1
            if n > _MAX_EXT:
                raise WMBusError("zu viele DIFE")
            dife = data[i]
            i += 1
            # Speichernummer nach Norm: n-tes DIFE um 1 + 4*(n-1) verschoben.
            # Abweichung von WMBus.pm: dort (Perl-Vorrang) um 4*n + 1 verschoben.
            storage |= (dife & 0x0F) << (1 + 4 * (n - 1))
            tariff |= ((dife >> 4) & 0x03) << (2 * (n - 1))
            subunit |= ((dife >> 6) & 0x01) << (n - 1)
            ext = dife & 0x80
        vib, i = _parse_vib(data, i, mfr)

        value: object
        if dlen_code == 0x0D:               # variable Länge (LVAR)
            lvar = data[i]
            i += 1
            if lvar <= 0xBF:
                raw = data[i : i + lvar]
                i += lvar
                if vib.quantity.startswith("manufacturer_specific"):
                    value = raw.hex()
                else:
                    # Norm: letztes Zeichen zuerst. Abweichung von WMBus.pm: dort nur bei
                    # Easymeter umgedreht; echte Telegramme (Schneider, Unismart) brauchen es immer.
                    value = _ascii(raw[::-1])
            elif lvar <= 0xDF:              # BCD, C0..CF positiv, D0..DF negativ
                nb = lvar & 0x0F
                raw = data[i : i + nb]
                i += nb
                num = _bcd(raw)
                value = None if num is None else (num if lvar < 0xD0 else -num)
                if isinstance(value, int) and vib.calc == "valueCalcNumeric":
                    value = _scale(value, vib.factor)
            elif lvar <= 0xEF:              # Binärzahl mit LVAR-E0 Bytes (WMBus.pm: Fehler)
                nb = lvar - 0xE0
                value = int.from_bytes(data[i : i + nb], "little", signed=True)
                i += nb
            else:
                raise WMBusError(f"LVAR {lvar:02X} nicht unterstützt")
        else:
            nb = _DATA_LEN[dlen_code]
            raw = data[i : i + nb]
            if len(raw) < nb:
                raise WMBusError("Daten abgeschnitten")
            i += nb
            if vib.quantity == "date" and nb == 2:
                value = _date_g(raw)
            elif vib.quantity == "datetime" and nb == 4:
                value = _datetime_f(raw)
            elif vib.quantity == "datetime" and nb == 6:
                value = _datetime_i(raw)
            elif vib.quantity in ("date", "datetime"):
                value = None
            elif nb == 0:
                value = None
            elif 9 <= dlen_code <= 14:
                value = _bcd(raw)
            elif dlen_code == 5:
                value = struct.unpack("<f", raw)[0]
            elif func == "error" and raw == b"\xff" * nb:
                value = None                # alle Bits gesetzt = ungültig
            else:
                # Ganzzahlen sind vorzeichenbehaftet (Typ B), wie in WMBus.pm
                value = int.from_bytes(raw, "little", signed=True)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if vib.calc == "valueCalcNumeric":
                    value = _scale(value, vib.factor)
                elif vib.calc == "valueCalcHex":
                    value = raw.hex()
                elif vib.calc == "valueCalcAscii":
                    value = str(value)
        records.append(
            Record(quantity=vib.quantity, unit=vib.unit, value=value, storage=storage,
                   tariff=tariff, function=func, subunit=subunit, vif_type=vib.vif_type,
                   extensions=tuple(vib.extensions))
        )


# ---------------------------------------------------------------- Verschlüsselung


def _aes(key: bytes):
    from cryptography.hazmat.primitives.ciphers import algorithms

    return algorithms.AES(key)


def _decrypt_cbc(enc: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC ohne Padding; ein Rest unter 16 Byte bleibt unverschlüsselt."""
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    usable = len(enc) - len(enc) % 16
    dec = Cipher(_aes(key), modes.CBC(iv)).decryptor()
    return dec.update(enc[:usable]) + dec.finalize() + enc[usable:]


def _decrypt_mode5(enc: bytes, key: bytes, iv: bytes) -> bytes:
    return _decrypt_cbc(enc, key, iv)


def _decrypt_ctr(enc: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    dec = Cipher(_aes(key), modes.CTR(iv)).decryptor()
    return dec.update(enc) + dec.finalize()


def cmac(key: bytes, data: bytes) -> bytes:
    """AES-CMAC (RFC 4493)."""
    from cryptography.hazmat.primitives.cmac import CMAC

    c = CMAC(_aes(key))
    c.update(data)
    return c.finalize()


def mode7_keys(key: bytes, mcr: bytes, ident: bytes) -> tuple[bytes, bytes]:
    """Kenc und Kmac nach EN 13757-7 / OMS (Zähler -> Gateway)."""
    tail = mcr + ident + b"\x07" * 7
    return cmac(key, b"\x00" + tail), cmac(key, b"\x01" + tail)


# ---------------------------------------------------------------- Telegramm


def _find_embedded(payload: bytes) -> bytes | None:
    """Eingepacktes Telegramm (Repeater) suchen: L-Feld passt exakt zur Restlänge."""
    for pos in range(len(payload) - 11):
        if payload[pos] == len(payload) - pos - 1 and payload[pos + 1] in (0x44, 0x46, 0x08, 0x06):
            return payload[pos:]
    return None


_ELL_LEN = {0x8C: 2, 0x8E: 10, 0x8D: 6, 0x8F: 14}


@dataclass
class _Afl:
    mcl: bytes = b""
    mcr: bytes = b""
    ml: bytes = b""
    mac: bytes = b""


def _parse_afl(afl: bytes) -> _Afl:
    """AFL-Felder (EN 13757-7, wie WMBus.pm decodeAFL)."""
    out = _Afl()
    fcl = int.from_bytes(afl[0:2], "little")
    if fcl & 0x4000:                         # weitere Fragmente folgen
        raise WMBusError("fragmentierte Telegramme werden nicht unterstützt")
    pos = 2
    mcl = 0
    if fcl & 0x2000:                         # MCL vorhanden
        out.mcl = afl[pos : pos + 1]
        mcl = out.mcl[0]
        pos += 1
    if fcl & 0x0800:                         # MCR vorhanden
        out.mcr = afl[pos : pos + 4]
        pos += 4
    if fcl & 0x1000:                         # ML vorhanden
        out.ml = afl[pos : pos + 2]
        pos += 2
    if fcl & 0x0400 and (mcl >> 2) & 0x03 == 1 and mcl & 0x03 == 1:
        out.mac = afl[pos : pos + 8]        # CMAC-AES128, 8 Byte
    return out


def decode(
    tele: bytes, keys: dict[str, bytes] | None = None, _depth: int = 0, *, header_only: bool = False
) -> Telegram:
    """Telegramm (ohne CRCs, beginnend mit L) auswerten.

    header_only: nur Kopfdaten (Hersteller, ID, Typ, Weiterleitung) – nichts
    entschlüsseln, keine Messwerte dekodieren (für nicht angelegte Zähler).
    """
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

    if ci == 0xA0 and _depth < 2 and len(tele) >= 30 and tele[19] == 0x44:
        # Über einen Netzwerkknoten weitergeleitet (C=C4, M=65 B2 = "LSE" + Bit 15):
        # ab Offset 18 steht das komplette Zählertelegramm. Dessen L-Feld stimmt nicht
        # immer, daher bis zum Ende des äußeren Telegramms auswerten. Die Absender-ID
        # des Knotens wechselt mit jedem Telegramm und wird verworfen.
        sub = decode(tele[18:], keys, _depth + 1, header_only=header_only)
        sub.relayed = True
        sub.inner = bytes(tele[18:])
        return sub

    def key_for(meter_id: str) -> bytes | None:
        return (keys or {}).get(meter_id)

    buf = tele[11:]                          # alles nach dem CI-Feld
    p = 0

    # --- Extended Link Layer
    if ci in _ELL_LEN:
        cc, t.access_no = buf[0], buf[1]
        sn = buf[2:6] if ci == 0x8D else buf[10:14] if ci == 0x8F else b""
        p = _ELL_LEN[ci]
        if sn:
            sn_int = int.from_bytes(sn, "little")
            rest = buf[p:]
            if sn_int >> 29:                 # verschlüsselt (AES-128-CTR)
                t.encrypted = True
                if header_only:
                    return t
                key = key_for(t.meter_id)
                if key is None:
                    return t
                iv = man_raw + a_field + bytes([cc]) + sn + b"\x00\x00\x00"
                rest = _decrypt_ctr(rest, key, iv)
            # Payload-CRC (LE) über den Rest des Rahmens.
            # Abweichung von WMBus.pm: dort nur L-20 Bytes geprüft.
            if int.from_bytes(rest[:2], "little") != crc16(rest[2:]):
                raise WMBusError(
                    "Payload-CRC (ELL) falsch" + (" – falscher Schlüssel?" if t.encrypted else ""),
                    t.meter_id,
                )
            buf, p = rest[2:], 0
            t.decrypted = t.encrypted
        if p >= len(buf):
            return t
        ci = buf[p]
        p += 1

    # --- Authentication and Fragmentation Layer
    afl: _Afl | None = None
    if ci == 0x90:
        afll = buf[p]
        afl = _parse_afl(buf[p + 1 : p + 1 + afll])
        p += 1 + afll
        ci = buf[p]
        p += 1
    tpl_start = p - 1                        # Index des TPL-CI-Felds (für die MAC)

    # --- Transport Layer
    id_raw = a_field[:4]
    if ci == 0x7A:                           # Kurzheader
        acc, st, cfg = buf[p], buf[p + 1], buf[p + 2 : p + 4]
        p += 4
        iv_head = man_raw + a_field
    elif ci == 0x72:                         # Langheader
        hid = buf[p : p + 4]
        hman = buf[p + 4 : p + 6]
        t.meter_id = hid[::-1].hex()
        t.manufacturer = manufacturer(int.from_bytes(hman, "little"))
        t.version, t.device_type = buf[p + 6], buf[p + 7]
        acc, st, cfg = buf[p + 8], buf[p + 9], buf[p + 10 : p + 12]
        p += 12
        id_raw = hid
        # IV aus dem Langheader (OMS); WMBus.pm nimmt immer die Sicherungsschicht.
        # Beides wird versucht, der Klartext (2F2F) entscheidet.
        iv_head = hman + hid + bytes([t.version, t.device_type])
    elif ci == 0x78:                         # ohne Header
        acc = st = None
        cfg = b"\x00\x00"
        iv_head = b""
    elif ci in (0x7E, 0x7F):
        if header_only:
            return t
        raise WMBusError("SML-kodierte Nutzdaten werden nicht unterstützt", t.meter_id)
    else:
        inner = _find_embedded(tele[11:]) if _depth < 2 and ci == tele[10] else None
        if inner is not None:
            sub = decode(inner, keys, _depth + 1, header_only=header_only)
            sub.via = t.meter_id
            sub.inner = bytes(inner)
            return sub
        if header_only:
            return t
        raise WMBusError(f"CI {ci:02X} wird nicht unterstützt")

    if acc is not None:
        t.access_no, t.status = acc, st
    # Sicherheitsmodus = Bits 8..12 des Konfigurationsworts; übrige Bits (z. B. 0x2000
    # "synchron") sagen nichts über Verschlüsselung aus
    mode = cfg[1] & 0x1F
    t.security_mode = mode
    if mode == 7:
        p += 1                               # Konfigurationswort hat 3 Byte
    payload = buf[p:]
    if header_only:
        t.encrypted = mode != 0
        return t
    if mode in (5, 7):
        t.encrypted = True
        key = key_for(t.meter_id)
        if key is None:
            return t                         # verschlüsselt, kein Schlüssel
        nblocks = cfg[0] >> 4
        enc_len = nblocks * 16 if nblocks else len(payload) - len(payload) % 16
        if mode == 5:
            plain = _decrypt_cbc(payload[:enc_len], key, iv_head + bytes([acc]) * 8)
            if plain[:2] != b"\x2f\x2f" and ci == 0x72:
                plain = _decrypt_cbc(payload[:enc_len], key, man_raw + a_field + bytes([acc]) * 8)
        else:
            if afl is None or len(afl.mcr) != 4:
                raise WMBusError("Modus 7 ohne AFL-Nachrichtenzähler", t.meter_id)
            kenc, kmac = mode7_keys(key, afl.mcr, id_raw)
            if afl.mac:
                mac = cmac(kmac, afl.mcl + afl.mcr + afl.ml + buf[tpl_start:])[:8]
                t.mac_valid = mac == afl.mac
            plain = _decrypt_cbc(payload[:enc_len], kenc, b"\x00" * 16)
        payload = plain + payload[enc_len:]
        if payload[:2] != b"\x2f\x2f":
            raise WMBusError("Entschlüsselung fehlgeschlagen (falscher Schlüssel?)", t.meter_id)
        t.decrypted = True
    elif mode != 0:
        t.encrypted = True
        return t
    t.records = parse_records(payload, t.manufacturer)
    return t
