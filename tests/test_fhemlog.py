"""FHEM-Log-Auswertung und Statistikplanung – ohne Home Assistant."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from .fhem_sim import REAL, hca, log_line, water
from .wmbus_loader import load

fhemlog = load("fhemlog")
TZ = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


def test_real_line():
    a = fhemlog.analyze([REAL], {"52143909": None}, TZ)
    assert (a.lines, a.decoded, sum(a.discarded.values())) == (1, 1, 0)
    m = a.meters["52143909"]
    assert (m.type_name, m.key, len(m.readings)) == ("Wasser", "volume", 1)
    r = m.readings[0]
    assert r.value == 56.178
    assert r.ts == datetime(2026, 11, 12, 4, 20, tzinfo=TZ) and r.from_meter       # Ablesezeit, nicht Empfang
    assert r.received == datetime(2026, 11, 12, 7, 40, 35, tzinfo=TZ)
    assert r.due == (date(2025, 12, 31), 17.965)
    assert r.rssi == -78.5 and r.tele.relayed


def test_foreign_and_bad_lines_discarded_without_trace():
    lines = [
        REAL,
        "2026.11.12 07:41:00 3: CUL_0: unknown message xyz",
        "kaputt",
        REAL.replace("9FE880", "9FE881"),                          # LQI anders – gleiche Ablesung
        REAL.replace("C13786105", "C13786106"),                   # CRC kaputt
    ]
    a = fhemlog.analyze(lines, {"11111111": None}, TZ)
    assert a.lines == 5 and a.decoded == 0 and a.meters == {}
    assert a.discarded == {fhemlog.R_FOREIGN: 2, fhemlog.R_FORMAT: 2, fhemlog.R_FRAME: 1}
    # keine ID des fremden Zählers in der Auswertung
    assert "52143909" not in repr(a)


def test_duplicates_backwards_and_missing_meter_time():
    t0 = datetime(2026, 3, 1, 6, 0)
    lines = [
        log_line(t0, water("52143909", 50000, t0)),
        log_line(t0 + timedelta(minutes=2), water("52143909", 50000, t0)),          # gleiche Ablesung erneut
        log_line(t0 + timedelta(days=1), water("52143909", 50100, t0 + timedelta(days=1))),
        log_line(t0 + timedelta(days=2), water("52143909", 49000, t0 + timedelta(days=2))),   # rückläufig
        log_line(t0 + timedelta(days=3), water("52143909", 50300, None)),           # ohne Ablesezeit
    ]
    a = fhemlog.analyze(lines, {"52143909": None}, TZ)
    m = a.meters["52143909"]
    assert [r.value for r in m.readings] == [50.0, 50.1, 50.3]
    assert a.discarded[fhemlog.R_DUPLICATE] == 1 and a.discarded[fhemlog.R_BACKWARDS] == 1
    assert len(m.anomalies) == 1 and "50.1 → 49" in m.anomalies[0]
    assert m.readings[-1].ts == (t0 + timedelta(days=3)).replace(tzinfo=TZ) and not m.readings[-1].from_meter


def test_hca_reset_at_due_date():
    due = date(2025, 12, 31)
    lines = [
        log_line(datetime(2025, 12, 30, 12), hca("16660259", 400, datetime(2025, 12, 30, 12), date(2024, 12, 31), 300)),
        log_line(datetime(2025, 12, 31, 12), hca("16660259", 430, datetime(2025, 12, 31, 12), date(2024, 12, 31), 300)),
        log_line(datetime(2026, 1, 2, 12), hca("16660259", 20, datetime(2026, 1, 2, 12), due, 450)),
        log_line(datetime(2026, 1, 3, 12), hca("16660259", 15, datetime(2026, 1, 3, 12), due, 450)),   # rückläufig
        log_line(datetime(2026, 1, 4, 12), hca("16660259", 30, datetime(2026, 1, 4, 12), due, 450)),
    ]
    a = fhemlog.analyze(lines, {"16660259": None}, TZ)
    m = a.meters["16660259"]
    assert [r.value for r in m.readings] == [400, 430, 20, 30]
    assert m.readings[2].reset and len(m.notes) == 1 and "Stichtags-Reset" in m.notes[0]
    assert len(m.anomalies) == 1                                    # 20 -> 15 ist kein Stichtag

    rows = fhemlog.daily_rows(m.readings, TZ)
    plan = fhemlog.plan_statistics(rows, [], None, 30.0, hca=True)
    sums = [r.sum for r in plan.rows]
    # Verbrauch: 30 (400->430) + 20 (430->450 bis Stichtag) + 20 (danach) + 10 = 80; letzte Summe 0
    assert sums == [-80.0, -50.0, -10.0, 0.0]


def _rows(*pairs, start=datetime(2026, 1, 1, 5, tzinfo=UTC)):
    return [fhemlog.Row(start + timedelta(days=d), float(s), day=(start + timedelta(days=d)).date())
            for d, s in pairs]


def _ex(*triples, start=datetime(2026, 1, 1, 5, tzinfo=UTC)):
    return [fhemlog.Row(start + timedelta(days=d, hours=h), float(s), float(u), existing=True)
            for d, h, s, u in triples]


def test_plan_without_statistics_ends_at_live_start():
    plan = fhemlog.plan_statistics(_rows((0, 10), (1, 12), (2, 15)), [], None, 15.0, hca=False)
    assert [(r.state, r.sum) for r in plan.rows] == [(10, -5), (12, -3), (15, 0)]
    assert plan.adjustments == []
    # Live-Wert schon höher (Import älter): Anschluss an den aktuellen Zustand
    plan = fhemlog.plan_statistics(_rows((0, 10), (1, 12)), [], None, 20.0, hca=False)
    assert [r.sum for r in plan.rows] == [-10, -8]


def test_plan_prefix_keeps_existing_sums():
    existing = _ex((5, 0, 100, 0), (5, 1, 101, 1), (6, 0, 110, 10))
    plan = fhemlog.plan_statistics(_rows((0, 90), (1, 95), (2, 97)), existing, None, None, hca=False)
    assert [r.sum for r in plan.rows] == [-10, -5, -3]       # 97 -> 100 = 3 bis zur ersten vorhandenen Stunde
    assert plan.adjustments == []


def test_plan_gap_and_adjustment():
    # vorhandene Statistik mit Lücke: Tag 0 und Tag 5; Summe am Tag 5 passt (HA hat nach der Lücke 30 addiert)
    existing = _ex((0, 0, 50, 10), (5, 0, 80, 40), (6, 0, 85, 45))
    plan = fhemlog.plan_statistics(_rows((2, 60), (3, 70)), existing, None, None, hca=False)
    assert [r.sum for r in plan.rows] == [20, 30] and plan.adjustments == []
    # Summe am Tag 5 zu niedrig (z. B. Reset erkannt) -> vorhandene Werte ab Tag 5 verschieben
    existing = _ex((0, 0, 50, 10), (5, 0, 80, 25), (6, 0, 85, 30))
    plan = fhemlog.plan_statistics(_rows((2, 60), (3, 70)), existing, None, None, hca=False)
    assert [r.sum for r in plan.rows] == [20, 30]
    assert plan.adjustments == [(existing[1].start, 15.0)]


def test_plan_existing_hours_and_overwrite():
    existing = _ex((0, 0, 50, 0), (1, 0, 60, 10), (3, 0, 80, 30))
    rows = _rows((1, 61), (2, 70), (3, 80), (4, 90))
    plan = fhemlog.plan_statistics(rows, existing, None, None, hca=False)
    assert [r.start for r in plan.rows] == [rows[1].start]                # Tag 1 vorhanden, Tag 3/4 nicht älter
    assert (plan.skipped_existing, plan.skipped_newer) == (1, 2)
    assert plan.rows[0].sum == 20 and plan.adjustments == []
    plan = fhemlog.plan_statistics(rows, existing, None, None, hca=False, overwrite=True)
    assert [(r.state, r.sum) for r in plan.rows] == [(61, 11), (70, 20)]
    assert plan.adjustments == []                                          # 70 -> 80: Summe 30 passt


def test_plan_short_term_only():
    short = fhemlog.Row(datetime(2026, 1, 10, 7, 15, tzinfo=UTC), 120.0, 2.0, existing=True)
    plan = fhemlog.plan_statistics(_rows((0, 100), (1, 110), (9, 119)), [], short, 121.0, hca=False)
    assert [r.sum for r in plan.rows] == [-18, -8, 1]                     # Anschluss an den 5-Minuten-Wert
    late = _rows((9, 119), start=datetime(2026, 1, 1, 9, tzinfo=UTC))   # 10.01. 09:00 – nach dem 5-Min-Wert
    assert fhemlog.plan_statistics(late, [], short, 121.0, hca=False).skipped_newer == 1


@pytest.mark.parametrize("line", [REAL, REAL.replace("::-78.5", "")])
def test_parse_line(line):
    received, raw, rssi = fhemlog.parse_line(line, TZ)
    assert raw.startswith("b4BC465") and raw.endswith("80")
    assert rssi == (-78.5 if "::" in line else None)
