"""Modul-Abfragen und -Einstellungen wie in FHEM (nur gegen den simulierten MapleCUN)."""

import pytest

from .wmbus_loader import load

culcmd = load("culcmd")


# ------------------------------------------------------------------ Umrechnungen (FHEM 00_CUL.pm)
def test_ccconf_defaults():
    """a-culfw CC1100_CFG: 0x21 0x65 0x6A / 0x57 / 0x07 / 0x90 -> wie FHEM 'get ccconf'."""
    conf = culcmd.ccconf({0x0D: 0x21, 0x0E: 0x65, 0x0F: 0x6A, 0x10: 0x57, 0x1B: 0x07, 0x1D: 0x90})
    assert conf == {"freq": 868.3, "bwidth": 325, "rampl": 42, "sens": 4}
    conf = culcmd.ccconf({0x0D: 0x10, 0x0E: 0xB0, 0x0F: 0x71, 0x10: 0xC7, 0x1B: 0x03, 0x1D: 0x93})
    assert conf == {"freq": 433.92, "bwidth": 101, "rampl": 33, "sens": 16}


def test_set_commands_like_fhem():
    assert culcmd.freq_cmds(433.92) == (["W0F10", "W10B0", "W1171"], 433.92)
    assert culcmd.freq_cmds(868.3)[0] == ["W0F21", "W1065", "W116A"]
    assert culcmd.bandwidths() == [812, 650, 541, 464, 406, 325, 270, 232, 203, 162, 135, 116, 101, 81, 67, 58]
    assert culcmd.bwidth_cmd(325, 0x57) == ("W1257", 325)
    assert culcmd.bwidth_cmd(330, 0x5A) == ("W125A", 325)        # untere 4 Bit bleiben
    assert culcmd.bwidth_cmd(10, 0x00) == ("W12F0", 58)          # kleinster Wert
    assert culcmd.rampl_cmd(42) == ("W1D07", 42)
    assert culcmd.rampl_cmd(41) == ("W1D06", 40)
    assert culcmd.rampl_cmd(24) == ("W1D00", 24)
    assert culcmd.sens_cmd(4) == ("W1F90", 4) and culcmd.sens_cmd(16) == ("W1F93", 16)
    assert culcmd.sens_cmd(6) == ("W1F90", 4)
    for bad in (lambda: culcmd.rampl_cmd(23), lambda: culcmd.sens_cmd(17), lambda: culcmd.patable_cmd("10")):
        with pytest.raises(ValueError):
            bad()
    assert culcmd.patable_cmd("08") == "x08"
    assert culcmd.led_cmd("blink") == "l02"
    assert culcmd.itclock_cmd(250) == "ic250"


def test_parse_replies():
    assert culcmd.parse_reg("C0D = 21 / 33") == (0x0D, 0x21)
    assert culcmd.parse_reg("C1D = 90 / 144") == (0x1D, 0x90)
    assert culcmd.parse_credit("21  900") == 900
    assert culcmd.parse_uptime("0001E240") == 987          # 123456 Ticks / 125
    assert culcmd.parse_uptime("b123") is None


def test_raw_blocked():
    assert culcmd.raw_blocked("B01") and culcmd.raw_blocked("e")
    assert not culcmd.raw_blocked("V") and not culcmd.raw_blocked("brs")


def test_real_device_replies():
    """Antworten des echten MapleCUN (a-culfw 1.26.08) vom 2026-11-14, Modul 2 (433 MHz) und Modul 1."""
    replies = ["C0D = 10 / 16", "C0E = B0 / 176", "C0F = 71 / 113", "C10 = 57 / 87",
               "C1B = 07 /  7", "C1D = 90 / 144"]
    regs = dict(culcmd.parse_reg(r) for r in replies)
    assert culcmd.ccconf(regs) == {"freq": 433.92, "bwidth": 325, "rampl": 42, "sens": 4}
    assert culcmd.parse_credit("21 3600") == 3600            # MAX_CREDIT 3600 (a-culfw 1.26.02)
    assert culcmd.parse_uptime("13CA0777") == 2656058         # ca. 30,7 Tage
