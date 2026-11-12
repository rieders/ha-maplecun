"""Simulierte FHEM-Logzeilen (synthetische, CRC-korrekte Telegramme) für die Import-Tests."""

from datetime import date, datetime

from .wmbus_loader import wmbus

# echtes Beispiel (Kaltwasser 52143909 über Netzwerkknoten, aus einem FHEM-Log)
REAL = (
    "2026.11.12 07:40:35 3: WMBUS Unknown device b4BC465B276900000F00D3781A0831100A03EC70039446532093914526DF"
    "51A077ACD0000000C13786105004C1365E7B7790100426C3F3C02BB560000326CFFFFD358046D14044C3B82046C5F3A8C0413"
    "1549692C05009FE880::-78.5, please define it"
)

LSE = bytes.fromhex("6532")
QDS = bytes.fromhex("9344")


def _bcd(value: int, nbytes: int) -> bytes:
    return bytes.fromhex(f"{value:0{2 * nbytes}d}")[::-1]


def _date_g(d: date) -> bytes:
    y = d.year - 2000
    return bytes([d.day | ((y & 7) << 5), d.month | ((y >> 3) << 4)])


def _datetime_f(t: datetime) -> bytes:
    y = t.year - 2000
    return bytes([t.minute, t.hour, t.day | ((y & 7) << 5), t.month | ((y >> 3) << 4)])


def telegram(meter_id: str, dtype: int, man: bytes, records: bytes, acc: int = 1) -> bytes:
    body = b"\x44" + man + bytes.fromhex(meter_id)[::-1] + bytes([0x01, dtype, 0x7A, acc, 0x00, 0x00, 0x00]) + records
    return bytes([len(body)]) + body


def cul_line(tele: bytes, lqi: str = "80") -> str:
    blocks = [tele[:10]] + [tele[i:i + 16] for i in range(10, len(tele), 16)]
    raw = b"".join(b + wmbus.crc16(b).to_bytes(2, "big") for b in blocks)
    return "b" + raw.hex().upper() + lqi


def water(meter_id: str, liters: int, when: datetime | None, due: date = date(2025, 12, 31),
          due_liters: int = 0) -> bytes:
    rec = b"\x0C\x13" + _bcd(liters, 4) + b"\x4C\x13" + _bcd(due_liters, 4) + b"\x42\x6C" + _date_g(due)
    if when is not None:
        rec += b"\x04\x6D" + _datetime_f(when)
    return telegram(meter_id, 0x07, LSE, rec)


def hca(meter_id: str, units: int, when: datetime, due: date, due_units: int) -> bytes:
    rec = (b"\x0B\x6E" + _bcd(units, 3) + b"\x42\x6C" + _date_g(due) + b"\x4B\x6E" + _bcd(due_units, 3)
           + b"\x04\x6D" + _datetime_f(when))
    return telegram(meter_id, 0x08, QDS, rec)


def log_line(received: datetime, tele: bytes, rssi: str = "-70") -> str:
    return f"{received:%Y.%m.%d %H:%M:%S} 3: WMBUS Unknown device {cul_line(tele)}::{rssi}, please define it"
