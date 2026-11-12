# SPDX-License-Identifier: GPL-2.0-only
"""VIF-Tabellen für den Wireless-M-Bus-Decoder.

Herkunft: FHEM ``FHEM/WMBus.pm`` (Autor: kaihs, FHEM-Forum), SVN-Revision 30375
vom 2025-10-11, Hashes %VIFInfo, %VIFInfo_FD, %VIFInfo_FB, %VIFInfo_other,
%VIFInfo_ESY, %VIFInfo_ESY2, %VIFInfo_KAM, %VIFInfo_KAM2. Maschinell übertragen
(Name, typeMask, expMask, type, bias, unit, calcFunc); Kommentare wie im Original.
Unveränderte Kopie des Originals: reference/WMBus.pm.

Ein Code passt auf einen Eintrag, wenn ``code & type_mask == type``; der Faktor ist
``10 ** ((code & exp_mask) + bias)``. Perl durchsucht die Hashes in zufälliger
Reihenfolge; hier gewinnt bei Überschneidungen der spezifischste Eintrag
(meiste Bits in type_mask) – betrifft nur FD 0x71 (Empfangspegel vs. reserviert).
"""

from __future__ import annotations

from typing import NamedTuple


class VifInfo(NamedTuple):
    """Ein Tabelleneintrag wie in WMBus.pm."""

    name: str
    type_mask: int
    exp_mask: int
    type: int
    bias: int
    unit: str
    calc: str | None          # Name der Perl-Funktion (valueCalcNumeric …) oder None


def lookup(table: tuple[VifInfo, ...], code: int) -> VifInfo | None:
    """Passenden Eintrag zum (7-Bit-)Code suchen, spezifischster zuerst."""
    code &= 0x7F
    hits = [e for e in table if code & e.type_mask == e.type]
    if not hits:
        return None
    return max(hits, key=lambda e: bin(e.type_mask).count("1"))


def factor(entry: VifInfo, code: int) -> float:
    return 10 ** ((code & entry.exp_mask) + entry.bias)


VIFINFO: tuple[VifInfo, ...] = (
    VifInfo('VIF_ENERGY_WATT', 0b01111000, 0b00000111, 0b00000000, -3, 'Wh', 'valueCalcNumeric'),  # 10(nnn-3) Wh  0.001Wh to 10000Wh
    VifInfo('VIF_ENERGY_JOULE', 0b01111000, 0b00000111, 0b00001000, 0, 'J', 'valueCalcNumeric'),  # 10(nnn) J     0.001kJ to 10000kJ
    VifInfo('VIF_VOLUME', 0b01111000, 0b00000111, 0b00010000, -6, 'm³', 'valueCalcNumeric'),  # 10(nnn-6) m3  0.001l to 10000l
    VifInfo('VIF_MASS', 0b01111000, 0b00000111, 0b00011000, -3, 'kg', 'valueCalcNumeric'),  # 10(nnn-3) kg  0.001kg to 10000kg
    VifInfo('VIF_ON_TIME_SEC', 0b01111111, 0b00000000, 0b00100000, 0, 'sec', 'valueCalcNumeric'),  # seconds
    VifInfo('VIF_ON_TIME_MIN', 0b01111111, 0b00000000, 0b00100001, 0, 'min', 'valueCalcNumeric'),  # minutes
    VifInfo('VIF_ON_TIME_HOURS', 0b01111111, 0b00000000, 0b00100010, 0, 'hours', None),  # hours
    VifInfo('VIF_ON_TIME_DAYS', 0b01111111, 0b00000000, 0b00100011, 0, 'days', None),  # days
    VifInfo('VIF_OP_TIME_SEC', 0b01111111, 0b00000000, 0b00100100, 0, 'sec', None),  # seconds
    VifInfo('VIF_OP_TIME_MIN', 0b01111111, 0b00000000, 0b00100101, 0, 'min', None),  # minutes
    VifInfo('VIF_OP_TIME_HOURS', 0b01111111, 0b00000000, 0b00100110, 0, 'hours', None),  # hours
    VifInfo('VIF_OP_TIME_DAYS', 0b01111111, 0b00000000, 0b00100111, 0, 'days', None),  # days
    VifInfo('VIF_ELECTRIC_POWER', 0b01111000, 0b00000111, 0b00101000, -3, 'W', 'valueCalcNumeric'),  # 10(nnn-3) W   0.001W to 10000W
    VifInfo('VIF_THERMAL_POWER', 0b01111000, 0b00000111, 0b00110000, 0, 'J/h', 'valueCalcNumeric'),  # 10(nnn) J/h   0.001kJ/h to 10000kJ/h
    VifInfo('VIF_VOLUME_FLOW', 0b01111000, 0b00000111, 0b00111000, -6, 'm³/h', 'valueCalcNumeric'),  # 10(nnn-6) m3/h 0.001l/h to 10000l/h
    VifInfo('VIF_VOLUME_FLOW_EXT1', 0b01111000, 0b00000111, 0b01000000, -7, 'm³/min', 'valueCalcNumeric'),  # 10(nnn-7) m3/min 0.0001l/min to 10000l/min
    VifInfo('VIF_VOLUME_FLOW_EXT2', 0b01111000, 0b00000111, 0b01001000, -9, 'm³/s', 'valueCalcNumeric'),  # 10(nnn-9) m3/s 0.001ml/s to 10000ml/s
    VifInfo('VIF_MASS_FLOW', 0b01111000, 0b00000111, 0b01010000, -3, 'kg/h', 'valueCalcNumeric'),  # 10(nnn-3) kg/h 0.001kg/h to 10000kg/h
    VifInfo('VIF_FLOW_TEMP', 0b01111100, 0b00000011, 0b01011000, -3, '°C', 'valueCalcNumeric'),  # 10(nn-3) °C 0.001°C to 1°C
    VifInfo('VIF_RETURN_TEMP', 0b01111100, 0b00000011, 0b01011100, -3, '°C', 'valueCalcNumeric'),  # 10(nn-3) °C 0.001°C to 1°C
    VifInfo('VIF_TEMP_DIFF', 0b01111100, 0b00000011, 0b01100000, -3, 'K', 'valueCalcNumeric'),  # 10(nn-3) K 1mK to 1000mK
    VifInfo('VIF_EXTERNAL_TEMP', 0b01111100, 0b00000011, 0b01100100, -3, '°C', 'valueCalcNumeric'),  # 10(nn-3) °C 0.001°C to 1°C
    VifInfo('VIF_PRESSURE', 0b01111100, 0b00000011, 0b01101000, -3, 'bar', 'valueCalcNumeric'),  # 10(nn-3) bar  1mbar to 1000mbar
    VifInfo('VIF_TIME_POINT_DATE', 0b01111111, 0b00000000, 0b01101100, 0, '', 'valueCalcDate'),  # data type G
    VifInfo('VIF_TIME_POINT_DATE_TIME', 0b01111111, 0b00000000, 0b01101101, 0, '', 'valueCalcDateTime'),  # data type F
    VifInfo('VIF_HCA', 0b01111111, 0b00000000, 0b01101110, 0, '', 'valueCalcNumeric'),  # Unit for Heat Cost Allocator, dimensonless
    VifInfo('VIF_FABRICATION_NO', 0b01111111, 0b00000000, 0b01111000, 0, '', 'valueCalcAscii'),  # Fabrication No
    VifInfo('VIF_OWNER_NO', 0b01111111, 0b00000000, 0b01111001, 0, '', None),  # Eigentumsnummer (used by Easymeter even though the standard allows this only for writing to a slave)
    VifInfo('VIF_AVERAGING_DURATION_SEC', 0b01111111, 0b00000000, 0b01110000, 0, 'sec', 'valueCalcNumeric'),  # seconds
    VifInfo('VIF_AVERAGING_DURATION_MIN', 0b01111111, 0b00000000, 0b01110001, 0, 'min', 'valueCalcNumeric'),  # minutes
    VifInfo('VIF_AVERAGING_DURATION_HOURS', 0b01111111, 0b00000000, 0b01110010, 0, 'hours', None),  # hours
    VifInfo('VIF_AVERAGING_DURATION_DAYS', 0b01111111, 0b00000000, 0b01110011, 0, 'days', None),  # days
    VifInfo('VIF_ACTUALITY_DURATION_SEC', 0b01111111, 0b00000000, 0b01110100, 0, 'sec', 'valueCalcNumeric'),  # seconds
    VifInfo('VIF_ACTUALITY_DURATION_MIN', 0b01111111, 0b00000000, 0b01110101, 0, 'min', 'valueCalcNumeric'),  # minutes
    VifInfo('VIF_ACTUALITY_DURATION_HOURS', 0b01111111, 0b00000000, 0b01110110, 0, 'hours', None),  # hours
    VifInfo('VIF_ACTUALITY_DURATION_DAYS', 0b01111111, 0b00000000, 0b01110111, 0, 'days', None),  # days
)

VIFINFO_FD: tuple[VifInfo, ...] = (
    VifInfo('VIF_CREDIT', 0b01111100, 0b00000011, 0b00000000, -3, '€', 'valueCalcNumeric'),  # Credit of 10nn-3 of the nominal local legal currency units
    VifInfo('VIF_DEBIT', 0b01111100, 0b00000011, 0b00000100, -3, '€', 'valueCalcNumeric'),  # Debit of 10nn-3 of the nominal local legal currency units
    VifInfo('VIF_ACCESS_NO', 0b01111111, 0b00000000, 0b00001000, 0, '', 'valueCalcNumeric'),  # Access number (transmission count)
    VifInfo('VIF_MEDIUM', 0b01111111, 0b00000000, 0b00001001, 0, '', 'valueCalcAscii'),  # Medium (as in fixed header)
    VifInfo('VIF_MANUFACTURER', 0b01111111, 0b00000000, 0b00001010, 0, '', 'valueCalcNumeric'),  # Manufacturer (as in fixed header)
    VifInfo('VIF_PARAMETER_SET_ID', 0b01111111, 0b00000000, 0b00001011, 0, '', 'valueCalcNumeric'),  # Parameter set identification
    VifInfo('VIF_MODEL_VERSION', 0b01111111, 0b00000000, 0b00001100, 0, '', 'valueCalcNumeric'),  # Model / Version
    VifInfo('VIF_HARDWARE_VERSION', 0b01111111, 0b00000000, 0b00001101, 0, '', 'valueCalcNumeric'),  # Hardware version #
    VifInfo('VIF_FIRMWARE_VERSION', 0b01111111, 0b00000000, 0b00001110, 0, '', 'valueCalcNumeric'),  # Firmware version #
    VifInfo('VIF_SOFTWARE_VERSION', 0b01111111, 0b00000000, 0b00001111, 0, '', 'valueCalcNumeric'),  # Software version #
    VifInfo('VIF_CUSTOMER_LOCATION', 0b01111111, 0b00000000, 0b00010000, 0, '', 'valueCalcHex'),  # Customer location
    VifInfo('VIF_CUSTOMER_CUSTOMER', 0b01111111, 0b00000000, 0b00010001, 0, '', 'valueCalcHex'),  # Customer
    VifInfo('VIF_ACCESS_CODE_USER', 0b01111111, 0b00000000, 0b00010010, 0, '', 'valueCalcHex'),  # Access code user
    VifInfo('VIF_ACCESS_CODE_OPERATOR', 0b01111111, 0b00000000, 0b00010011, 0, '', 'valueCalcHex'),  # Access code operator
    VifInfo('VIF_ACCESS_CODE_SYSTEM_OPERATOR', 0b01111111, 0b00000000, 0b00010100, 0, '', 'valueCalcHex'),  # Access code system operator
    VifInfo('VIF_PASSWORD', 0b01111111, 0b00000000, 0b00010110, 0, '', 'valueCalcHex'),  # Password
    VifInfo('VIF_ERROR_FLAGS', 0b01111111, 0b00000000, 0b00010111, 0, '', 'valueCalcHex'),  # Error flags (binary)
    VifInfo('VIF_DURATION_SINCE_LAST_READOUT', 0b01111100, 0b00000011, 0b00101100, 0, 's', 'valueCalcTimeperiod'),  # Duration since last readout [sec(s)..day(s)]
    VifInfo('VIF_VOLTAGE', 0b01110000, 0b00001111, 0b01000000, -9, 'V', 'valueCalcNumeric'),  # 10nnnn-9 Volts
    VifInfo('VIF_ELECTRICAL_CURRENT', 0b01110000, 0b00001111, 0b01010000, -12, 'A', 'valueCalcNumeric'),  # 10nnnn-12 Ampere
    VifInfo('VIF_RECEPTION_LEVEL', 0b01111111, 0b00000000, 0b01110001, 0, 'dBm', 'valueCalcNumeric'),  # reception level of a received radio device.
    VifInfo('VIF_STATE_PARAMETER_ACTIVATION', 0b01111111, 0b00000000, 0b01100110, 0, '', 'valueCalcNumeric'),  # State of parameter activation
    VifInfo('VIF_DURATION_IN_MINUTES', 0b01111111, 0b00000000, 0b00110001, 0, 'minutes', 'valueCalcNumeric'),  # DURATION_IN_MINUTES information
    VifInfo('VIF_CUMULATION_COUNTER', 0b01111111, 0b00000000, 0b01100001, 0, '', 'valueCalcNumeric'),  # Cumulated Value
    VifInfo('VIF_SPECIAL_SUPPLIER_INFORMATION', 0b01111111, 0b00000000, 0b01100111, 0, '', 'valueCalcNumeric'),  # Special supplier information
    VifInfo('VIF_FD_RESERVED', 0b01110000, 0b00000000, 0b01110000, 0, 'Reserved', None),  # Reserved
)

VIFINFO_FB: tuple[VifInfo, ...] = (
    VifInfo('VIF_ENERGY', 0b01111110, 0b00000001, 0b00000000, -1, 'MWh', 'valueCalcNumeric'),  # Energy 10(n-1) MWh  0.1MWh to 1MWh
)

VIFINFO_OTHER: tuple[VifInfo, ...] = (
    VifInfo('VIF_ERROR_NONE', 0b01111111, 0b00000000, 0b00000000, 0, 'No error', None),
    VifInfo('VIF_TOO_MANY_DIFES', 0b01111111, 0b00000000, 0b00000001, 0, 'Too many DIFEs', None),
    VifInfo('VIF_ILLEGAL_VIF_GROUP', 0b01111111, 0b00000000, 0b00001100, 0, 'Illegal VIF-Group', None),
    VifInfo('VIF_DATA_UNDERFLOW', 0b01111111, 0b00000000, 0b00010111, 0, 'Data underflow', None),
    VifInfo('VIF_PER_SECOND', 0b01111111, 0b00000000, 0b00100000, 0, 'per second', None),
    VifInfo('VIF_PER_MINUTE', 0b01111111, 0b00000000, 0b00100001, 0, 'per minute', None),
    VifInfo('VIF_PER_HOUR', 0b01111111, 0b00000000, 0b00100010, 0, 'per hour', None),
    VifInfo('VIF_PER_DAY', 0b01111111, 0b00000000, 0b00100011, 0, 'per day', None),
    VifInfo('VIF_PER_WEEK', 0b01111111, 0b00000000, 0b00100100, 0, 'per week', None),
    VifInfo('VIF_PER_MONTH', 0b01111111, 0b00000000, 0b00100101, 0, 'per month', None),
    VifInfo('VIF_PER_YEAR', 0b01111111, 0b00000000, 0b00100110, 0, 'per year', None),
    VifInfo('VIF_PER_REVOLUTION', 0b01111111, 0b00000000, 0b00100111, 0, 'per revolution/measurement', None),
    VifInfo('VIF_PER_INCREMENT_INPUT', 0b01111110, 0b00000000, 0b00101000, 0, 'increment per input pulse on input channnel #', 'valueCalcNumeric'),
    VifInfo('VIF_PER_INCREMENT_OUTPUT', 0b01111110, 0b00000000, 0b00101010, 0, 'increment per output pulse on output channnel #', 'valueCalcNumeric'),
    VifInfo('VIF_PER_LITER', 0b01111111, 0b00000000, 0b00101100, 0, 'per liter', None),
    VifInfo('VIF_PER_M3', 0b01111111, 0b00000000, 0b00101101, 0, 'per m³', None),
    VifInfo('VIF_PER_KG', 0b01111111, 0b00000000, 0b00101110, 0, 'per kg', None),
    VifInfo('VIF_PER_K', 0b01111111, 0b00000000, 0b00101111, 0, 'per K', None),
    VifInfo('VIF_PER_KWH', 0b01111111, 0b00000000, 0b00110000, 0, 'per kWh', None),
    VifInfo('VIF_PER_GJ', 0b01111111, 0b00000000, 0b00110001, 0, 'per GJ', None),
    VifInfo('VIF_PER_KW', 0b01111111, 0b00000000, 0b00110010, 0, 'per kW', None),
    VifInfo('VIF_PER_KL', 0b01111111, 0b00000000, 0b00110011, 0, 'per (K*l)', None),
    VifInfo('VIF_PER_V', 0b01111111, 0b00000000, 0b00110100, 0, 'per V', None),
    VifInfo('VIF_PER_A', 0b01111111, 0b00000000, 0b00110101, 0, 'per A', None),
    VifInfo('VIF_PER_MULT_S', 0b01111111, 0b00000000, 0b00110110, 0, 'multiplied by sek', None),
    VifInfo('VIF_PER_MULT_SV', 0b01111111, 0b00000000, 0b00110111, 0, 'multiplied by sek / V', None),
    VifInfo('VIF_PER_MULT_SA', 0b01111111, 0b00000000, 0b00111000, 0, 'multiplied by sek / A', None),
    VifInfo('VIF_START_DATE_TIME', 0b01111111, 0b00000000, 0b00111001, 0, 'start date(/time) of', None),
    VifInfo('VIF_ACCUMULATION_IF_POSITIVE', 0b01111111, 0b00000000, 0b00111011, 0, 'Accumulation only if positive contribution', None),
    VifInfo('VIF_DURATION_NO_EXCEEDS', 0b01110111, 0b00000000, 0b01000001, 0, '# of exceeds', 'valueCalcu'),
    VifInfo('VIF_DURATION_LIMIT_EXCEEDED', 0b01110000, 0b00000000, 0b01010000, 0, 'duration of limit exceeded', 'valueCalcufnn'),
    VifInfo('VIF_MULTIPLICATIVE_CORRECTION_FACTOR', 0b01111000, 0b00000111, 0b01110000, -6, '', None),
    VifInfo('VIF_MULTIPLICATIVE_CORRECTION_FACTOR_1000', 0b01111111, 0b00000000, 0b01111101, 0, '', 'valueCalcMultCorr1000'),
    VifInfo('VIF_FUTURE_VALUE', 0b01111111, 0b00000000, 0b01111110, 0, '', None),
    VifInfo('VIF_MANUFACTURER_SPECIFIC', 0b01111111, 0b00000000, 0b01111111, 0, 'manufacturer specific', None),
)

VIFINFO_ESY: tuple[VifInfo, ...] = (
    VifInfo('VIF_ELECTRIC_POWER_PHASE', 0b01000000, 0b00000000, 0b00000000, -2, 'W', 'valueCalcNumeric'),
)

VIFINFO_ESY2: tuple[VifInfo, ...] = (
    VifInfo('VIF_ELECTRIC_POWER_PHASE_NO', 0b01111110, 0b00000000, 0b00101000, 0, 'phase #', 'valueCalcNumeric'),
)

VIFINFO_KAM: tuple[VifInfo, ...] = (
    VifInfo('VIF_KAMSTRUP_INFO', 0b00000000, 0b00000000, 0b00000000, 0, '', None),
)

VIFINFO_KAM2: tuple[VifInfo, ...] = (
    VifInfo('VIF_TARGET_OR_REVERSE_VOLUME', 0b00010000, 0b00000111, 0b00010000, -6, 'm³/h', 'valueCalcNumeric'),  # 10(nnn-6) m3/h 0.001l/h to 10000l/h
)
