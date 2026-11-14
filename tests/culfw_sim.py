"""Simulierter MapleCUN mit a-culfw 1.26.08 (vier CC1101) für die Tests.

Nachgebildet nach dem a-culfw-Quelltext (github.com/heliflieger/a-culfw, Stand des Builds
323 vom 2019-08-03) und den Antworten des echten Geräts:

* "V"                -> Versionszeile
* "?"/unbekannt      -> "? (<cmd> is unknown) Use one of …"     (clib/multi_CC.c)
* "brs"/"brt"        -> S/T-Modus, Antwort SMODE/TMODE            (clib/rf_mbus.c)
* "brc", "brx"       -> aus, Antwort OFF (a-culfw hat keinen C-Modus); set_RF_mode(off)
                        setzt dabei die GLOBALE Variable mbus_mode = WMBUS_NONE und beendet
                        damit den wMBus-Empfang auf jedem Modul          (clib/rf_mode.c:192)
* "br", "b"          -> nur Status – global (mbus_mode/radio_mode), nicht je Modul:
                        am echten Gerät antwortete Modul 3 "TMODE", als Modul 1 auf T stand
* wMBus auf einem Modul schaltet jedes andere wMBus-Modul ab     (clib/rf_mode.c:170)
* "X21"              -> SlowRF (beendet wMBus auf diesem Modul), keine Antwort; "X" -> "21  900"
* "Cxx"              -> "Cxx = HH / DDD"                         (clib/cc1100.c, ccreg)
* "t"                -> 8 Hex-Ziffern (Ticks à 8 ms)              (clib/clock.c)
* "Wxxyy"            -> EEPROM schreiben (nur protokolliert)
"""

from __future__ import annotations

VERSION = "V 1.26.08 a-culfw Build: 323 (2019-08-03_09-32-54) MapleCUNx4_8F (F-Band: 868MHz) CUL"

# Kommandotabellen fntab, fntab1, fntab2, fntab3 aus Devices/MapleCUN/main.c (Auszug)
CMDS = {
    0: "B b C F i A Z N E G M K L U Y R T V W X e f m l t x z *",
    1: "b C F i A Z N E G M K L U Y R T V W X f x z *",
    2: "b C F i A Z N E G M K L U Y R T V W X f x z *",
    3: "C A Z N E L Y V X f z",
}

# CC1100_CFG (clib/cc1100.c): 868,3 MHz, 325 kHz (0x57), 42 dB, 4 dB
DEFAULT_REGS = {0x0D: 0x21, 0x0E: 0x65, 0x0F: 0x6A, 0x10: 0x57, 0x1B: 0x07, 0x1D: 0x90}


class Firmware:
    """Gemeinsamer Zustand aller vier Module (eine MapleCUN-Firmware)."""

    def __init__(self) -> None:
        self.mode = {i: "off" for i in range(4)}     # CC1101.RF_mode je Instanz
        self.mbus = "off"                             # globales mbus_mode
        self.txreport = {i: 0 for i in range(4)}
        self.regs = {i: dict(DEFAULT_REGS) for i in range(4)}
        self.eeprom: list[tuple[int, str]] = []        # (Instanz, "Wxxyy")
        self.eeprom_regs = {i: {} for i in range(4)}   # geschriebene CC1101-Register je Instanz
        self.sent: list[tuple[int, str]] = []          # alle Kommandos (Instanz, Kommando)
        self.credit = 900
        self.ticks = 0x0001E240
        self.cmds = dict(CMDS)
        self.silent = False                            # True: antwortet auf nichts

    def _status(self, inst: int) -> str:
        return {"wmbus_s": "SMODE", "wmbus_t": "TMODE"}.get(self.mbus, "OFF")

    def receiving(self) -> dict[int, str]:
        """Instanzen, die tatsächlich wMBus empfangen (RF_mode wMBus und mbus_mode gesetzt)."""
        return {i: m for i, m in self.mode.items() if m.startswith("wmbus") and self.mbus == m}

    def handle(self, inst: int, cmd: str) -> list[str]:
        if cmd:
            self.sent.append((inst, cmd))
        if self.silent or not cmd:
            return []
        letters = self.cmds[inst].split()
        if cmd[0] not in letters:
            return [f"? ({cmd} is unknown) Use one of {self.cmds[inst]}"]
        c = cmd[0]
        if c == "V":
            return [VERSION]
        if c == "b":
            if len(cmd) > 2 and cmd[1] == "r":
                new = {"s": "wmbus_s", "t": "wmbus_t"}.get(cmd[2], "off")
                if new != "off":
                    for other in self.mode:              # rf_mode.c: nur ein wMBus-Modul
                        if other != inst and self.mode[other].startswith("wmbus"):
                            self.mode[other] = "off"
                self.mode[inst] = new
                self.mbus = new
            return [self._status(inst)]
        if c == "X":
            if len(cmd) == 1:
                return [f"{self.txreport[inst]:02X}{self.credit:5d}"]
            self.txreport[inst] = int(cmd[1:3], 16)
            self.mode[inst] = "slow" if self.txreport[inst] else "off"
            if self.mbus == "off":                       # set_txrestore: nicht bei aktivem wMBus
                self.regs[inst].update(self.eeprom_regs[inst])
            return []
        if c == "C" and len(cmd) == 3:
            reg = int(cmd[1:], 16)
            val = self.regs[inst].get(reg, 0)
            return [f"C{reg:02X} = {val:02X} / {val:2d}"]
        if c == "t":
            return [f"{self.ticks:08X}"]
        if c == "W":
            self.eeprom.append((inst, cmd))
            addr, val = int(cmd[1:3], 16), int(cmd[3:5], 16)
            if 0x02 <= addr < 0x02 + 0x29:               # EE_CC1100_CFG + Register
                self.eeprom_regs[inst][addr - 2] = val
            return []
        return []
