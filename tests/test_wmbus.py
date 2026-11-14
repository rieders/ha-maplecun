"""Wireless-M-Bus-Decoder – ohne Home Assistant lauffähig (lädt wmbus.py direkt)."""

from datetime import date, datetime

import pytest

from .wmbus_loader import wmbus

# echte Zeilen aus dem FHEM-Log (MapleCUN), mit gültigen Block-CRCs und dem LQI-Byte;
# das RSSI-Byte hat FHEM (00_CUL.pm) abgeschnitten
DIRECT_WARM = (
    "b2D446532179929601A06FE067A830000000C13708009004C132865009C1000426C3F3C02BB560000"
    "326CFFFF046D30A31A00413BD45380"
)
RELAYED_HCA = (
    "b54C465B276904000F0BA583AA0831000B059C3003144934459026616E7C536087AA30000200B6E88"
    "04004B6E5102038300426C3F3CCB086E860400C2086C5F3A1EAE326CFFFF046D170D413B2F04ED39"
    "1F0B2B86283801FD0C1002FD3CC201356980"
)


def _decode(line: str):
    frame, rssi = wmbus.parse_cul_line(line)
    tele = wmbus.decode(frame)
    tele.rssi = rssi
    return tele


def _flip(line: str, pos: int) -> str:
    """Ein Hex-Zeichen an Position pos (nach dem "b") verändern."""
    i = pos + 1
    return line[:i] + ("0" if line[i] != "0" else "1") + line[i + 1:]


# ------------------------------------------------------------------ CRC
def test_crc16_en13757():
    # Prüfwert der CRC-16/EN-13757 für "123456789"
    assert wmbus.crc16(b"123456789") == 0xC2B7


def test_crc_valid_on_real_lines():
    for line in (DIRECT_WARM, RELAYED_HCA):
        frame, _ = wmbus.parse_cul_line(line)
        assert len(frame) == frame[0] + 1


@pytest.mark.parametrize("line", [DIRECT_WARM, RELAYED_HCA])
@pytest.mark.parametrize("pos", [4, 30, 60])          # erster, zweiter, dritter Block
def test_crc_error_rejected(line, pos):
    with pytest.raises(wmbus.WMBusError, match="CRC"):
        wmbus.parse_cul_line(_flip(line, pos))


# ------------------------------------------------------------------ a) direkt, Warmwasser
def test_direct_warm_water():
    t = _decode(DIRECT_WARM)
    assert (t.manufacturer, t.meter_id, t.device_type) == ("LSE", "60299917", 0x06)
    assert not t.encrypted and not t.relayed
    v = t.values()
    assert v["volume"] == 98.070
    assert v["volume_s1"] == 6.528 and v["date_s1"] == date(2025, 12, 31)
    assert v["datetime"] == datetime(2026, 11, 1, 0, 26)
    assert t.month_storage == 8


# ------------------------------------------------------------------ b) weitergeleitet, HKV
def test_relayed_heat_cost_allocator():
    t = _decode(RELAYED_HCA)
    # ID, Hersteller und Typ aus dem inneren Kopf, nicht vom Netzwerkknoten (LSE 00409076)
    assert (t.manufacturer, t.meter_id, t.device_type) == ("QDS", "16660259", 0x08)
    assert t.relayed and t.via is None
    assert not t.encrypted                       # Config 0x2000: Modus 0, nur "synchron"-Bit
    v = t.values()
    assert v["hca"] == 488
    assert v["hca_s1"] == 251 and v["date_s1"] == date(2025, 12, 31)
    assert t.month_storage == 17
    assert v["hca_s17"] == 486 and v["date_s17"] == date(2026, 10, 31)
    assert v["datetime"] == datetime(2026, 11, 1, 13, 23)
    # Datensätze hinter dem inneren L-Feld werden gelesen, unbekannte VIFEs nicht verwechselt
    assert "datetime_x39" in v and v["datetime"] != v["datetime_x39"]


def test_relayed_inner_length_not_trusted():
    """Inneres L-Feld (0x31) ist kürzer als der Rest – trotzdem alles bis zum Ende."""
    frame, _ = wmbus.parse_cul_line(RELAYED_HCA)
    assert frame[18] == 0x31 and len(frame) - 18 > frame[18] + 1
    # abgeschnittener letzter Datensatz wird toleriert
    cut = frame[:-1]
    t = wmbus.decode(bytes([len(cut) - 1]) + cut[1:])
    assert t.meter_id == "16660259" and t.values()["hca"] == 488


# ------------------------------------------------------------------ Rohformat
@pytest.mark.parametrize("line", [DIRECT_WARM, RELAYED_HCA])
def test_trailing_bytes_lqi_rssi(line):
    """Zeilen aus dem FHEM-Log: nur LQI (0x80 = 128), RSSI dort als '::-64' abgetrennt.
    Direkt vom MapleCUN (X21): LQI und RSSI; 0x14 -> 20/2 - 74 = -64 dBm."""
    frame, rssi = wmbus.parse_cul_line(line)
    assert rssi is None
    assert wmbus.cul_status(b"\x80") == (128, None)
    frame2, rssi = wmbus.parse_cul_line(line + "14")
    assert frame2 == frame and rssi == -64.0
    assert wmbus.cul_status(b"\x80\x14") == (128, -64.0)
    frame3, rssi = wmbus.parse_cul_line(line[:-2])        # ohne angehängte Bytes
    assert frame3 == frame and rssi is None
    _, rssi = wmbus.parse_cul_line(line + "14::-64")      # FHEM-Anhang wird ignoriert
    assert rssi == -64.0
    with pytest.raises(wmbus.WMBusError, match="unerwartete"):
        wmbus.parse_cul_line(line + "1400")


def test_trailing_bytes_not_part_of_telegram():
    frame, _ = wmbus.parse_cul_line(DIRECT_WARM + "14")
    assert frame.hex().endswith("1a00413b")      # Telegramm endet vor CRC, LQI und RSSI
    assert wmbus.cul_status(b"\x80\xF0") == (128, -82.0)   # Wert >= 128: (Wert-256)/2-74


def test_frame_b_recognized():
    """'bY' wird als Rahmen B erkannt und nicht als Rahmen A gelesen."""
    with pytest.raises(wmbus.WMBusError):
        wmbus.parse_cul_line("bY" + DIRECT_WARM[1:])


def test_not_wmbus():
    with pytest.raises(wmbus.WMBusError):
        wmbus.parse_cul_line("V 1.26 a-culfw")
    with pytest.raises(wmbus.WMBusError):
        wmbus.parse_cul_line("bXYZ")


# ------------------------------------------------------------------ Datensätze
def test_storage_number_dife():
    # DIF 0xC4: Speicher-Bit 6 = 1, DIFE 0x88 (Speicher 8 -> <<1) , DIFE 0x01 (1 -> <<5), VIF 0x13
    recs = wmbus.parse_records(bytes([0xC4, 0x88, 0x01, 0x13, 0x10, 0x27, 0x00, 0x00]))
    assert recs[0].storage == 1 + (8 << 1) + (1 << 5)
    assert recs[0].value == 10.0


def test_records_filler_unknown_and_manufacturer():
    data = bytes([
        0x2F, 0x2F,                    # Füllbytes
        0x0B, 0x6E, 0x88, 0x04, 0x00,  # BCD 6 Stellen, HKV 488
        0x0C, 0x13, 0x78, 0x56, 0x34, 0x12,  # BCD 8 Stellen, 12345.678 m³
        0x02, 0xBB, 0x56, 0x00, 0x00,  # VIF mit unbekannter Erweiterung
        0x01, 0x7A, 0x05,              # VIF ohne Eintrag in den Tabellen (Busadresse)
        0x0F, 0x01, 0x02, 0x03,        # Rest herstellerspezifisch
    ])
    recs = {r.key: r.value for r in wmbus.parse_records(data)}
    assert recs["hca"] == 488
    assert recs["volume"] == 12345.678
    assert "volume_flow_x56" in recs and "volume_flow" not in recs
    assert recs["vif_7a"] == 5
    assert recs["manufacturer_data"] == "010203"
    assert len(recs) == 5


def test_dates():
    assert wmbus._date_g(bytes.fromhex("3f3c")) == date(2025, 12, 31)
    assert wmbus._date_g(bytes.fromhex("ffff")) is None
    assert wmbus._datetime_f(bytes.fromhex("170d413b")) == datetime(2026, 11, 1, 13, 23)


def test_encrypted_without_key_only_flagged():
    frame, _ = wmbus.parse_cul_line(DIRECT_WARM)
    enc = bytearray(frame)
    enc[14] = 0x05                                 # Config: Modus 5
    t = wmbus.decode(bytes(enc))
    assert t.encrypted and not t.records and t.meter_id == "60299917"


# ------------------------------------------------------------------ Zeile direkt vom Gerät
# Am 2026-11-14 mit a-culfw 1.26.08 (Build 323) im T-Modus (X21, brt) direkt am Split-Proxy
# mitgeschnitten. Fremder Zähler, deshalb anonymisiert: A-Feld (ID) durch 12345678 ersetzt,
# Block-CRCs neu berechnet; alles andere – auch die angehängten Bytes 80 2A – unverändert.
DEVICE_T_LINE = (
    "b32446850785634126980A999A0919F330000E0160000A407B30700009D9A0000000000000000000000"
    "0000000000FFFF000000000000000000FFFF802A"
)


def test_device_line_lqi_rssi_order():
    """a-culfw clib/rf_mbus.c hängt DH2(lqi); DH2(rssi) an. Gerät: 80 2A.

    LQI-Byte des CC1101 (Bit 7 CRC_OK) = 0x80, RSSI 0x2A = 42/2 - 74 = -53 dBm.
    Umgekehrt gelesen wäre RSSI 0x80 = -138 dBm – unplausibel.
    """
    raw = bytes.fromhex(DEVICE_T_LINE[1:])
    assert len(raw) == 0x32 + 1 + 4 * 2 + 2       # L-Feld + 4 Block-CRCs + LQI + RSSI
    frame, rssi = wmbus.parse_cul_line(DEVICE_T_LINE)
    assert rssi == -53.0
    assert wmbus.cul_status(raw[-2:]) == (0x80, -53.0)
    head = wmbus.decode(frame, header_only=True)
    assert (head.manufacturer, head.meter_id) == ("TCH", "12345678")
    with pytest.raises(wmbus.WMBusError):         # Block-CRC wird geprüft
        wmbus.parse_cul_line(_flip(DEVICE_T_LINE, 30))


# Eigener Warmwasserzähler 52144701, am 2026-11-14 10:18 im S-Modus (X21, brs) über den
# Netzwerkknoten direkt am Split-Proxy empfangen – unverändert, mit LQI- und RSSI-Byte.
DEVICE_S_LINE = (
    "b4BC465B276900000F086DC0FA08311007D3E96003944653201471452B7021A067ADA0000000C1378950200"
    "4C13426DED990000426C3F3C02BB560000326CFFFF7615046D2F074E3B82046C5F3A8C04137087CEB30200"
    "2B2A800B"
)


def test_device_line_own_meter_s_mode():
    frame, rssi = wmbus.parse_cul_line(DEVICE_S_LINE)
    raw = bytes.fromhex(DEVICE_S_LINE[1:])
    assert wmbus.cul_status(raw[-2:]) == (0x80, -68.5)     # LQI 0x80, RSSI 0x0B = 11/2 - 74
    assert rssi == -68.5
    t = wmbus.decode(frame)
    assert (t.manufacturer, t.meter_id, t.type_name, t.relayed) == ("LSE", "52144701", "Warmwasser", True)
    assert t.inner[:2].hex() == "3944"                      # inneres Telegramm: L=0x39, C=0x44
    v = t.values()
    assert v["volume"] == 29.578 and v["volume_s1"] == 9.942 and v["volume_s8"] == 28.77
    assert str(v["date_s1"]) == "2025-12-31" and str(v["date_s8"]) == "2026-10-31"
    assert str(v["datetime"]) == "2026-11-14 07:47:00"
