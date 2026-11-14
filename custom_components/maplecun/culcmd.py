"""a-culfw-Kommandos und Umrechnungen für Modul-Abfragen und -Einstellungen.

Kommandos und Formeln aus FHEM 00_CUL.pm (CUL_Get/CUL_Set, $Id: 00_CUL.pm 24815
2021-08-01), geprüft gegen den a-culfw-Quelltext (github.com/heliflieger/a-culfw):

* "V"   Version                                   clib/fncollection.c  version()
* "?"   Kommandoliste ("… Use one of …")          clib/multi_CC.c      callfn2()
* "t"   Laufzeit, 8 Hex-Ziffern, Ticks à 8 ms      clib/clock.c         gettime()
* "X"   TX_REPORT + credit_10ms ("21  900")       clib/rf_receive.c    set_txreport()
* "Cxx" CC1101-Register lesen ("C0D = 21 / 33")   clib/cc1100.c        ccreg()
* "Wxxyy" EEPROM schreiben; Adresse 0x02 + Register (EE_CC1100_CFG), auf Modul 2 und 3
        auf deren eigenen Bereich umgelenkt (EE_CC1100_CFG1/2)  clib/fncollection.c write_eeprom()
* "x0N" PA-Tabelle N ins EEPROM (nur der Bereich von Modul 1!) und Chip neu laden
        (ccInitChip(EE_CC1100_CFG))               clib/cc1100.c        ccsetpa()/cc_set_pa()
* "l0N" LED aus/an/blinken, wird im EEPROM gespeichert (ewb(EE_LED))  ledfunc()
* "icN" Intertechno-Pulslänge in µs, nur im RAM (Vorgabe 420)  clib/intertechno.c it_func()
"""

from __future__ import annotations

import re

# FHEM: my @ampllist = (24, 27, 30, 33, 36, 38, 40, 42); # rAmpl(dB)
AMPL_LIST = (24, 27, 30, 33, 36, 38, 40, 42)
SENS_LIST = (4, 8, 12, 16)
CCCONF_REGS = (0x0D, 0x0E, 0x0F, 0x10, 0x1B, 0x1D)        # FHEM: %r = ("0D","0E","0F","10","1B","1D")

# PA-Tabelle (a-culfw clib/cc1100.c CC1100_PA, FULL_CC1100_PA beim MapleCUN)
PATABLE = {
    "00": "-10 dBm (Rampe)", "01": "-5 dBm (Rampe)", "02": "0 dBm (Rampe)",
    "03": "5 dBm (Rampe)", "04": "10 dBm (Rampe)",
    "05": "-10 dBm", "06": "-5 dBm", "07": "0 dBm", "08": "5 dBm", "09": "10 dBm",
}
LED_MODES = {"off": "00", "on": "01", "blink": "02"}       # FHEM: aus (00), an (01), blinkend (02)
ITCLOCK_DEFAULT = 250                                      # FHEM-Vorgabe; a-culfw startet mit 420

_REG_RE = re.compile(r"^C([0-9A-Fa-f]{2}) = ([0-9A-Fa-f]{2}) /")
_CREDIT_RE = re.compile(r"^.. *(\d*)\s*$")
_UPTIME_RE = re.compile(r"^[0-9A-F]{8}$")


def bandwidths() -> list[int]:
    """Erlaubte Bandbreiten in kHz, in der Reihenfolge der FHEM-Schleife (groß -> klein)."""
    return [int(26000 / (8 * (4 + m) * (1 << e))) for e in range(4) for m in range(4)]


# ---------------------------------------------------------------- Lesen
def parse_reg(line: str) -> tuple[int, int] | None:
    """'C0D = 21 / 33' -> (0x0D, 0x21)."""
    m = _REG_RE.match(line)
    return (int(m[1], 16), int(m[2], 16)) if m else None


def parse_credit(line: str) -> int | None:
    """Antwort auf 'X' -> credit10ms (FHEM: /^.. *(\\d*)[\\r\\n]*$/)."""
    m = _CREDIT_RE.match(line)
    return int(m[1]) if m and m[1] else None


def parse_uptime(line: str) -> int | None:
    """Antwort auf 't' -> Sekunden (FHEM: hex($msg)/125)."""
    return int(line, 16) // 125 if _UPTIME_RE.match(line) else None


def ccconf(regs: dict[int, int]) -> dict[str, float]:
    """FHEM 'get ccconf': freq (MHz), bWidth (kHz), rAmpl (dB), sens (dB)."""
    r = regs
    return {
        "freq": round(26 * ((r[0x0D] * 256 + r[0x0E]) * 256 + r[0x0F]) / 65536, 3),
        "bwidth": int(26000 / (8 * (4 + ((r[0x10] >> 4) & 3)) * (1 << ((r[0x10] >> 6) & 3)))),
        "rampl": AMPL_LIST[r[0x1B] & 7],
        "sens": 4 + 4 * (r[0x1D] & 3),
    }


# ---------------------------------------------------------------- Schreiben (EEPROM)
def freq_cmds(mhz: float) -> tuple[list[str], float]:
    """FHEM 'set freq': W0F/W10/W11 (FREQ2..0) und die tatsächlich eingestellte Frequenz."""
    f = mhz / 26 * 65536
    f2 = int(f / 65536)
    f1 = int(int(f % 65536) / 256)
    f0 = int(f) % 256                      # Perl: $f % 256 rechnet ganzzahlig
    actual = round((f2 * 65536 + f1 * 256 + f0) / 65536 * 26, 3)
    return [f"W0F{f2:02X}", f"W10{f1:02X}", f"W11{f0:02X}"], actual


def bwidth_cmd(khz: float, old_mdmcfg4: int) -> tuple[str, int]:
    """FHEM 'set bWidth': erste Bandbreite <= Wunsch; untere 4 Bit von MDMCFG4 bleiben."""
    bits, bw = 0, 0
    for e in range(4):
        for m in range(4):
            bits = (e << 6) + (m << 4)
            bw = int(26000 / (8 * (4 + m) * (1 << e)))
            if khz >= bw:
                return f"W12{(old_mdmcfg4 & 0x0F) + bits:02X}", bw
    return f"W12{(old_mdmcfg4 & 0x0F) + bits:02X}", bw


def rampl_cmd(db: int) -> tuple[str, int]:
    """FHEM 'set rAmpl' (24..42): größter Listenwert <= Wunsch -> W1D0N."""
    if not 24 <= db <= 42:
        raise ValueError("rAmpl: 24 bis 42 dB")
    v = 0
    while v < len(AMPL_LIST) and AMPL_LIST[v] <= db:
        v += 1
    return f"W1D{v - 1:02d}", AMPL_LIST[v - 1]


def sens_cmd(db: int) -> tuple[str, int]:
    """FHEM 'set sens' (4..16): int(arg/4)*4 -> W1F9N."""
    if not 4 <= db <= 16:
        raise ValueError("sens: 4 bis 16 dB")
    return f"W1F9{int(db / 4) - 1}", int(db / 4) * 4


def patable_cmd(idx: str) -> str:
    if idx not in PATABLE:
        raise ValueError("patable: 00 bis 09")
    return f"x{idx}"


def led_cmd(mode: str) -> str:
    return f"l{LED_MODES[mode]}"


def itclock_cmd(us: int) -> str:
    if not 1 <= us <= 65535:
        raise ValueError("ITClock: 1 bis 65535 µs")
    return f"ic{int(us)}"


def raw_blocked(command: str) -> bool:
    """send_raw: 'B…' (Bootloader/Neustart) und 'e…' (Werkseinstellungen) nur mit force."""
    return command[:1] in ("B", "e")
