# SPDX-License-Identifier: GPL-2.0-only
"""Tests für die Portierung von FHEM WMBus.pm – ohne Home Assistant lauffähig.

Jeder Test trägt eine Herkunftsmarke:

  ECHT   echtes Telegramm aus öffentlicher Quelle, erwartete Werte aus derselben Quelle
  SPEC   Prüfwert aus einer Spezifikation (RFC 4493, OMS Vol. 2 Annex N)
  PERL   nur gegen die Logik bzw. Tabellen von WMBus.pm geprüft; die Eingabe ist
         konstruiert, ein echtes Telegramm dafür lag nicht vor

Quellen der Telegramme (nur Rohdaten und erwartete Messwerte, kein Code):
  [WM]  wmbusmeters, https://github.com/wmbusmeters/wmbusmeters, Commit f6ab1c28
        (drivers/src/*.xmq, simulations/serial_aes.msg, tests/test_frame_b_trim.sh,
        src/testinternals.cc)
  [OMS] OMS Specification Vol. 2, Annex N (Beispiel Sicherheitsmodus 7); Chiffretext
        aus dem Kommentar in WMBus.pm decrypt_mode7, Kenc wie in [WM] testinternals.cc
"""

from datetime import date, datetime
import re

import pytest

from .wmbus_loader import REFERENCE, tables, wmbus

pytest.importorskip("cryptography")


def _decode(hexstr: str, meter_id: str | None = None, key: str | None = None):
    keys = {meter_id: bytes.fromhex(key)} if key else None
    return wmbus.decode(bytes.fromhex(hexstr), keys)


# ====================================================================== AES-CMAC / Modus 7


def test_cmac_rfc4493():
    """SPEC: Prüfvektoren aus RFC 4493 (auch in [WM] testinternals.cc)."""
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    assert wmbus.cmac(key, b"").hex() == "bb1d6929e95937287fa37d129b756746"
    assert wmbus.cmac(key, bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")).hex() == (
        "070a16b46b4d4144f79bdd9dd04a287c"
    )


def test_mode7_key_derivation_oms_annex_n():
    """SPEC: Kenc aus OMS Annex N; Chiffretext aus dem Kommentar in WMBus.pm.

    Das vollständige Telegramm des Beispiels liegt nicht vor, daher werden nur
    Schlüsselableitung und Entschlüsselung geprüft; die Messwerte sind aus dem
    entschlüsselten Klartext berechnet.
    """
    key = bytes.fromhex("000102030405060708090A0B0C0D0E0F")
    kenc, _ = wmbus.mode7_keys(key, mcr=bytes.fromhex("b30a0000"), ident=bytes.fromhex("78563412"))
    assert kenc.hex().upper() == "ECCF39D475D730B8284FDFDC1995D52F"
    enc = bytes.fromhex("9058475F4BC91DF878B80A1B0F98B629024AAC727942BFC549233C0140829B93")
    plain = wmbus._decrypt_cbc(enc, kenc, b"\x00" * 16)
    assert plain.hex().startswith("2f2f0c1427048502046d32371f1502fd170000")
    v = {r.key: r.value for r in wmbus.parse_records(plain)}
    assert v["volume"] == 28504.27
    assert v["datetime"] == datetime(2008, 5, 31, 23, 50)


# ====================================================================== Rahmen B + ELL + AFL + Modus 7

# [WM] tests/test_frame_b_trim.sh: Sensostar (EFE), Rahmen B mit Sicherungsschicht-CRCs, L=0xC2
# (zwei CRC-Blöcke), ELL CI 8C, AFL CI 90 mit MAC, TPL CI 7A, Sicherheitsmodus 7
SENSOSTAR = (
    "C244C5140443054300048C200E900F002C25895E02003B0877D31DA840B17A0E009007100F8FAD4C4E61"
    "400DA4F3B1F7E273CC4C5C07F93CD20B237345DB39F9A98F8415E1C4B7A25C86E4C22D05C3352FA7E2E5"
    "627031F14643063F1DC8FE2C32459B6F02E4234B447B18E4BFDA8C4DA9B1EDE6CF29D0B2122A3E8B9DEE"
    "BEE3E6DBE6617B4E709EC6B595179CB01C2C0BDE6723A345B708652880C114A58DA07C73AD9EB8ACB17F"
    "A0A88F19BC84CB1FC56672DE8A9603FD0C05010002FD0B2111F60B"
)
SENSOSTAR_KEY = {"43054304": bytes.fromhex("622b9656991ff0c1574c0950cf9278d1")}


def test_frame_b_ell_afl_mode7_real():
    """ECHT [WM]: erwartet total_kwh=9341, total_water_m3=1348.631."""
    frame, _ = wmbus.parse_cul_line("bY" + SENSOSTAR)
    assert len(frame) == 0xC2 + 1 - 4            # beide CRCs entfernt
    t = wmbus.decode(frame, SENSOSTAR_KEY)
    assert (t.manufacturer, t.meter_id, t.device_type) == ("EFE", "43054304", 0x04)
    assert t.security_mode == 7 and t.encrypted and t.decrypted
    assert t.mac_valid is True                    # AFL-MAC mit Kmac nachgerechnet
    v = t.values()
    assert v["energy"] == 9341
    assert v["volume"] == 1348.631


def test_frame_b_crc_error_in_second_block():
    """ECHT [WM], ein Byte im zweiten CRC-Block verfälscht."""
    bad = SENSOSTAR[:300] + ("0" if SENSOSTAR[300] != "0" else "1") + SENSOSTAR[301:]
    with pytest.raises(wmbus.WMBusError, match="CRC"):
        wmbus.parse_cul_line("bY" + bad)


def test_mode7_without_or_with_wrong_key():
    frame, _ = wmbus.parse_cul_line("bY" + SENSOSTAR)
    t = wmbus.decode(frame)
    assert t.encrypted and not t.decrypted and not t.records
    with pytest.raises(wmbus.WMBusError, match="Schlüssel") as err:
        wmbus.decode(frame, {"43054304": bytes(16)})
    assert err.value.meter_id == "43054304"


def test_mode7_mac_mismatch_detected():
    """PERL+: MAC-Prüfung (in WMBus.pm nicht vorhanden) erkennt eine verfälschte MAC."""
    frame, _ = wmbus.parse_cul_line("bY" + SENSOSTAR)
    pos = frame.index(bytes.fromhex("3B0877D31DA840B1"))
    forged = frame[:pos] + b"\x00" + frame[pos + 1:]
    t = wmbus.decode(forged, SENSOSTAR_KEY)
    assert t.mac_valid is False and t.values()["energy"] == 9341


def test_frame_b_single_block():
    """PERL: Rahmen B mit nur einem Block (L <= 127); CRC selbst berechnet nach WMBus.pm."""
    tele = bytes.fromhex("314493441234567835087a740000200b6e2701004b6e450100426c5f2ccb086e790000c2086c7f21326cffff046d200b7422")
    body = bytes([len(tele) + 1]) + tele[1:]      # L zählt im Rahmen B die CRC mit
    raw = body + wmbus.crc16(body).to_bytes(2, "big") + b"\x80\x40"   # LQI, RSSI
    frame, rssi = wmbus.parse_cul_line("bY" + raw.hex())
    assert frame[1:] == tele[1:] and rssi == -42.0
    assert wmbus.decode(frame).values()["hca"] == 127


# ====================================================================== ELL 8D (AES-CTR)

# [WM] simulations/serial_aes.msg: Kamstrup Multical 21, ELL CI 8D, verschlüsselt (Sitzungsnummer)
KAMSTRUP = "2A442D2C998734761B168D2091D37CAC21E1D68CDAFFCD3DC452BD802913FF7B1706CA9E355D6C2701CC24"
KAMSTRUP_KEY = "28F64A24988064A079AA2C807D6102AE"


def test_ell_8d_aes_ctr_real():
    """ECHT [WM]: total_m3=6.408, target_m3=6.408, min. Vorlauf 127 °C, min. Außen 19 °C."""
    t = _decode(KAMSTRUP, "76348799", KAMSTRUP_KEY)
    assert (t.manufacturer, t.meter_id, t.device_type) == ("KAM", "76348799", 0x16)
    assert t.encrypted and t.decrypted
    v = t.values()
    assert v["volume"] == 6.408 and v["volume_s1"] == 6.408
    assert v["flow_temperature_min_s1"] == 127
    assert v["external_temperature_min_s1"] == 19
    # Kamstrup-Herstellertabelle (VIFInfo_KAM): 02 FF 20 = Info-Register
    assert v["kamstrup_info"] == 0x71


def test_ell_payload_crc_covers_whole_rest():
    """ECHT [WM]: Payload-CRC über den ganzen Rest. WMBus.pm prüft nur L-20 Byte und
    käme hier auf einen anderen Wert – Abweichung bewusst."""
    raw = bytes.fromhex(KAMSTRUP)
    iv = raw[2:10] + raw[11:12] + raw[13:17] + b"\x00" * 3
    rest = wmbus._decrypt_ctr(raw[17:], bytes.fromhex(KAMSTRUP_KEY), iv)
    stored = int.from_bytes(rest[:2], "little")
    assert wmbus.crc16(rest[2:]) == stored
    assert wmbus.crc16(rest[2 : 2 + raw[0] - 20]) != stored


def test_ell_8d_wrong_or_missing_key():
    with pytest.raises(wmbus.WMBusError, match="Payload-CRC"):
        _decode(KAMSTRUP, "76348799", "00" * 16)
    t = _decode(KAMSTRUP)
    assert t.encrypted and not t.decrypted and not t.records


# ====================================================================== Modus 5, Kurz- und Langheader


@pytest.mark.parametrize(
    ("hexstr", "meter_id", "key", "expected"),
    [
        pytest.param(  # [WM] kadenwater.xmq
            "7e446d147856341201077a0d007005c927ee43f1102ba4944b43501e2368a0f18a01c1b0d0ef02e69b4451a2"
            "08ee5be8c419577228b50d39ef0ac642349b2e91b01ec233116fb7fb643e83d78dc039296bf261f23600d9a68ada"
            "548aac1945ac8f9f7fbe3b7db1c37d3c35dc34bc97bcb9586d5f292c8d53e20c0d2ccf2dde",
            "12345678", "9F5213BC13841410BB1410141515E4D5",
            {"volume": 727.7096, "volume_flow_max": 2.594},
            id="CI7A-kadenwater",
        ),
        pytest.param(  # [WM] picoflux.xmq
            "6e446d141949545605077adf0060055ad21c5cafec3a71c0720754f2ca777e238d0318d35a2de0f8fe592e9a0d"
            "0cacbd61dfd2815c21a7fa743d93b5e543847e3e67b5d80816a253db655d4354b1a06c1fb7bae99e0fa322bff37a"
            "05fd651c5e3c09968ae4df0d8d911fee33ef31b9",
            "56544919", "9F5213BC13841410BB1410141515E4D5",
            {"volume": 4.492, "datetime": datetime(2026, 2, 9, 8, 57)},
            id="CI7A-picoflux",
        ),
        pytest.param(  # [WM] unismart.xmq – auch FD-Tabelle und Klartext rückwärts
            "6044B8059430040001037A1D005085E2B670BCF1A5C87E0C1A51DA18924EF984613DA2A9CD39D8F4C7208326C7"
            "6D42DBEADF80D574192B71BD7C4F56A7F1513151768A9DB804883B28CB085CA2D0F7438C361CB9E2734712ED9BFB"
            "B2A14EF55208",
            "00043094", "00000000000000000000000000000000",
            {"volume_x3a": 917, "volume_x3a_s1": 911.32, "model_version": "  4GGU",
             "datetime": datetime(2021, 9, 15, 13, 18), "datetime_s1": datetime(2021, 9, 1, 6, 0)},
            id="CI7A-unismart",
        ),
        pytest.param(  # [WM] ebeco500.xmq – ELL 8C vor dem Kurzheader, Datum/Zeit Typ I
            "31444514e301000001028c20807a2e002025e25dea4f2bc8ec94920260443a95c2bf3856e3e380e889cc8233ca"
            "5d9771f289",
            "000001e3", "FCC9B808216D9D1F5FF6E1A3CF7D3084",
            {"energy": 68.6, "external_temperature": 22.8, "on_time_min": 6338,
             "datetime": datetime(2000, 2, 1, 13, 54, 50)},
            id="ELL8C-ebeco500",
        ),
        pytest.param(  # [WM] iwmtx5.xmq – ELL 8C vor dem Kurzheader
            "5144b4097073912218078c00247a0308400571e9615249ede52eaae09f61908f027c3877f3330ae9079528b231"
            "73ce124bcc255393e60b173c0a9f274c42dd92e4b23c14e8a41f042903358df01dd9268ad4",
            "22917370", "00000000000000000000000000000000",
            {"volume": 0.025, "datetime": datetime(2023, 5, 11, 10, 38, 24)},
            id="ELL8C-iwmtx5",
        ),
        pytest.param(  # [WM] aventieswm.xmq – Langheader CI 72
            "76442104710007612507727100076121042507B5006005E2E95A3C2A1279A5415E6732679B43369FD5FDDDD7"
            "83EEEBB48236D34E7C94AF0A18A5FDA5F7D64111EB42D4D891622139F2952F9D12A20088DFA4CF8123871123"
            "EE1F6C1DCEA414879DDB4E05E508F1826D7EFBA6964DF804C9261EA23BBF03",
            "61070071", "A004EB23329A477F1DD2D7820B56EB3D",
            {"volume": 466.472, "volume_s1": 465.96, "volume_s7": 415.23, "volume_s14": 357.84},
            id="CI72-aventieswm",
        ),
    ],
)
def test_mode5_real(hexstr, meter_id, key, expected):
    """ECHT [WM]: Werte wie im erwarteten JSON von wmbusmeters."""
    t = _decode(hexstr, meter_id, key)
    assert t.meter_id == meter_id and t.security_mode == 5 and t.decrypted
    v = t.values()
    for k, val in expected.items():
        assert v[k] == val, k
    with pytest.raises(wmbus.WMBusError, match="Schlüssel"):
        _decode(hexstr, meter_id, "11" * 16)


def test_long_header_unencrypted_real():
    """ECHT [WM] elf.xmq: Langheader CI 72 ohne Verschlüsselung, viele VIF-Arten."""
    t = _decode(
        "51440186010905001837721956880101064004DA000020026CA9220E017799241103000C13641320000A2D0000"
        "0A5A90060A5E800544050E77000001FD0C010A6564370AFD4731030A274907047F00000002"
    )
    assert (t.meter_id, t.manufacturer) == ("01885619", "APA")   # aus dem Langheader
    v = t.values()
    assert v["date"] == date(2021, 2, 9)
    assert v["energy"] == 3112.49977
    assert v["volume"] == 201.364
    assert v["flow_temperature"] == 69 and v["return_temperature"] == 58
    assert v["energy_s1"] == 3047.8
    assert v["external_temperature"] == 37.64
    assert v["voltage"] == 3.31                    # FD-Tabelle
    assert v["op_time_days"] * 24 == 17976         # wmbusmeters: operating_time_h


# ====================================================================== DIF/VIF mit echten Telegrammen


def test_storage_number_norm_real():
    """ECHT [WM] qcaloric.xmq: DIF CB DIFE 08 = Speicher 17 (WMBus.pm rechnet 257)."""
    v = _decode("314493441234567835087a740000200b6e2701004b6e450100426c5f2ccb086e790000c2086c7f21326cffff046d200b7422").values()
    assert v["hca"] == 127 and v["hca_s1"] == 145 and v["date_s1"] == date(2018, 12, 31)
    assert v["hca_s17"] == 79 and v["date_s17"] == date(2019, 1, 31)
    assert v["datetime"] == datetime(2019, 2, 20, 11, 32)


def test_signed_integers_real():
    """ECHT [WM] c5isf.xmq: INT32 vorzeichenbehaftet (prev_*_month_kwh = -2147483648)."""
    t = _decode(
        "E544496A55554455880D7A320200002F2F04060000000004130000000002FD17240084800106000000008280016C"
        "2124C480010600000080C280016CFFFF"
    )
    v = t.values()
    assert v["energy_s32"] == 0 and v["date_s32"] == date(2017, 4, 1)
    assert v["energy_s33"] == -2147483648 and isinstance(v["energy_s33"], int)
    assert v["error_flags"] == "2400"              # FD 17, valueCalcHex


def test_fd_ascii_and_vife_real():
    """ECHT [WM] abbb23.xmq: FD 0E Firmware als Zeichenkette (rückwärts übertragen)."""
    t = _decode(
        "844442040011223320027A3E0000200E840017495200000004FFA0150000000004FFA1150000000004FFA215"
        "0000000004FFA3150000000007FFA600000000000000000007FFA700000000000000000007FFA80000000000"
        "0000000007FFA90000000000000000000DFD8E0007302E38322E31420DFFAA000B3030312D313131203332421F"
    )
    recs = {r.key: r for r in t.records}
    assert recs["energy_x00"].value == 5249.17     # Energie mit VIFE 00 ("kein Fehler")
    assert recs["energy_x00"].extensions == ("VIF_ERROR_NONE",)
    assert recs["firmware_version_x00"].value == "B1.28.0"
    assert recs["firmware_version_x00"].vif_type == "VIF_FIRMWARE_VERSION"


# ====================================================================== Nur gegen WMBus.pm


def _vib(*codes: int, mfr: str = "") -> wmbus._Vib:
    vib, _ = wmbus._parse_vib(bytes(codes) + b"\x00" * 4, 0, mfr)
    return vib


def test_vif_fb_energy_mwh():
    """PERL: %VIFInfo_FB, 10^(n-1) MWh -> kWh."""
    recs = wmbus.parse_records(bytes([0x04, 0xFB, 0x01, 0x0C, 0x00, 0x00, 0x00]))
    assert recs[0].key == "energy" and recs[0].value == 12000 and recs[0].vif_type == "VIF_ENERGY"


def test_vif_fd_timeperiod_and_overlap():
    """PERL: valueCalcTimeperiod (Einheit aus Exponent); FD 0x71 überschneidet sich in
    WMBus.pm mit 'reserviert' – hier gewinnt der spezifische Eintrag."""
    assert _vib(0xFD, 0x2E).unit == "h"
    assert _vib(0xFD, 0x2C).unit == "s"
    assert _vib(0xFD, 0x71).vif_type == "VIF_RECEPTION_LEVEL"
    assert _vib(0xFD, 0x72).vif_type == "VIF_FD_RESERVED"


def test_vife_correction_factors():
    """PERL: VIFE 0x7D = Korrektur *1000 (valueCalcMultCorr1000); VIFE 0x70..0x77 =
    10^(nnn-6) – in WMBus.pm ohne Wirkung, hier nach Norm angewendet."""
    recs = wmbus.parse_records(bytes([0x02, 0x93, 0x7D, 0x05, 0x00, 0x02, 0x93, 0x75, 0x05, 0x00]))
    assert recs[0].value == 5.0 and recs[0].extensions == ("VIF_MULTIPLICATIVE_CORRECTION_FACTOR_1000",)
    assert recs[1].value == 0.0005


def test_plaintext_vif():
    """PERL: decodePlaintext – VIF 7C, Länge, Einheit rückwärts."""
    recs = wmbus.parse_records(bytes([0x01, 0x7C, 0x03]) + b"hWk" + bytes([0x2A]))
    assert recs[0].unit == "kWh" and recs[0].value == 42


def test_manufacturer_tables_esy_kam():
    """PERL: %VIFInfo_ESY (Leistung je Phase, 10^-2 W) und %VIFInfo_KAM2 (Ziel-/Rückwärtsvolumen)."""
    recs = wmbus.parse_records(bytes([0x04, 0xFF, 0x00, 0x10, 0x27, 0x00, 0x00]), "ESY")
    assert recs[0].vif_type == "VIF_ELECTRIC_POWER_PHASE" and recs[0].value == 100.0
    assert _vib(0x93, 0x3C, mfr="KAM").extensions == ["VIF_TARGET_OR_REVERSE_VOLUME"]
    assert _vib(0x93, 0x3C).extensions == ["VIFE_3C"]      # ohne Kamstrup unbekannt


def test_lvar_bcd_and_subunit():
    """PERL: LVAR C0..DF (BCD positiv/negativ); DIFE-Bit 6 = Untereinheit."""
    recs = wmbus.parse_records(bytes([0x0D, 0x13, 0xC2, 0x34, 0x12, 0x0D, 0x13, 0xD1, 0x05,
                                      0x84, 0x40, 0x13, 0x01, 0x00, 0x00, 0x00]))
    assert [r.value for r in recs[:2]] == [1.234, -0.005]
    assert recs[2].subunit == 1 and recs[2].key == "volume_u1"


def test_afl_fragment_and_sml_rejected():
    """PERL: fragmentierte AFL und SML-Nutzdaten (CI 7E/7F) werden abgelehnt."""
    head = bytes.fromhex("2E4493440102030401088C2000")
    with pytest.raises(wmbus.WMBusError, match="fragment"):
        wmbus.decode(head + bytes.fromhex("90020040") + bytes.fromhex("7a000000000000"))
    with pytest.raises(wmbus.WMBusError, match="SML"):
        wmbus.decode(bytes.fromhex("2E449344010203040108" "7E" "0000000000"))


# ====================================================================== Tabellen = WMBus.pm


@pytest.mark.skipif(not REFERENCE.exists(), reason="reference/WMBus.pm fehlt")
def test_tables_match_reference_module():
    """PERL: die VIF-Tabellen stimmen Eintrag für Eintrag mit reference/WMBus.pm überein."""
    src = REFERENCE.read_text(encoding="utf-8")
    for perl_name, ours in [
        ("VIFInfo", tables.VIFINFO), ("VIFInfo_FD", tables.VIFINFO_FD), ("VIFInfo_FB", tables.VIFINFO_FB),
        ("VIFInfo_other", tables.VIFINFO_OTHER), ("VIFInfo_ESY", tables.VIFINFO_ESY),
        ("VIFInfo_ESY2", tables.VIFINFO_ESY2), ("VIFInfo_KAM", tables.VIFINFO_KAM),
        ("VIFInfo_KAM2", tables.VIFINFO_KAM2),
    ]:
        body = re.search(r"^my %" + perl_name + r" = \((.*?)^\);", src, re.S | re.M).group(1)
        entries = re.findall(r"^\s*(\w+)\s*=>\s*\{(.*?)\}", body, re.S | re.M)
        assert [n for n, _ in entries] == [e.name for e in ours], perl_name
        for (name, fields), e in zip(entries, ours):
            f = dict(re.findall(r"(\w+)\s*=>\s*([^,\n]+)", fields))
            assert int(f["typeMask"].strip(), 0) == e.type_mask, name
            assert int(f["expMask"].strip(), 0) == e.exp_mask, name
            assert int(f["type"].strip(), 0) == e.type, name
            assert int(f["bias"].strip()) == e.bias, name
            assert f["unit"].strip().strip("'") == e.unit, name
            assert (f.get("calcFunc", "").strip().lstrip("\\&") or None) == e.calc, name
