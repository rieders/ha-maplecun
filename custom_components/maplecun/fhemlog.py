# SPDX-License-Identifier: GPL-2.0-only
"""FHEM-Log mit Wireless-M-Bus-Telegrammen auswerten – ohne Home-Assistant-Abhängigkeiten.

Erwartetes Zeilenformat (FHEM, Gerät nicht definiert)::

    2026.07.04 05:56:56 3: WMBUS Unknown device b<HEX>::-56.5, please define it

<HEX> ist die Rohzeile von a-culfw mit einem Restbyte (LQI), die Signalstärke
steht hinter "::". Der Zeitstempel ist die Empfangszeit in lokaler Zeit.

Hier wird nur ausgewertet und geplant; geschrieben wird in importer.py.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
import re
from typing import Iterable

try:
    from . import wmbus
except ImportError:  # Tests ohne Paket
    import wmbus  # type: ignore[no-redef]

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\s+\d+:\s+WMBUS Unknown device\s+"
    r"(?P<raw>bY?[0-9A-Fa-f]+)(?:::(?P<rssi>-?\d+(?:\.\d+)?))?"
)

# Zählerstand, aus dem Verlauf und Statistik gebildet werden (Reihenfolge = Vorrang)
MAIN_KEYS = ("hca", "volume", "energy")
UNITS = {"volume": "m³", "energy": "kWh", "hca": None}
UNIT_CLASSES = {"volume": "volume", "energy": "energy", "hca": None}

R_FORMAT = "Zeilenformat nicht erkannt"
R_FRAME = "Rahmen oder CRC fehlerhaft"
R_DECODE = "nicht dekodierbar"
R_FOREIGN = "fremder Zähler (nicht angelegt)"
R_ENCRYPTED = "verschlüsselt, Schlüssel fehlt oder falsch"
R_NOVALUE = "kein Zählerstand im Telegramm"
R_DUPLICATE = "doppelt (gleiche Ablesezeit und gleicher Stand)"
R_BACKWARDS = "rückläufiger Stand"


@dataclass
class Reading:
    """Ein Zählerstand aus dem Log."""

    meter_id: str
    ts: datetime                # Ablesezeit (aus dem Telegramm, sonst Empfangszeit), mit Zeitzone
    received: datetime          # Empfangszeit laut Log, mit Zeitzone
    from_meter: bool            # Ablesezeit stammt aus dem Telegramm
    key: str                    # volume / hca / energy
    value: float
    values: dict
    tele: wmbus.Telegram
    rssi: float | None
    reset: bool = False         # Heizkostenverteiler: Rücksprung am Stichtag

    @property
    def due(self) -> tuple[date | None, float | None]:
        """Stichtag und Stichtagswert aus dem Telegramm."""
        d = self.values.get("date_s1")
        v = self.values.get(f"{self.key}_s1")
        if isinstance(d, datetime):
            d = d.date()
        return (d if isinstance(d, date) else None, v if isinstance(v, (int, float)) else None)


@dataclass
class MeterImport:
    meter_id: str
    type_name: str
    manufacturer: str
    key: str
    readings: list[Reading] = field(default_factory=list)       # plausibel, nach Zeit sortiert
    anomalies: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    lines: int = 0
    decoded: int = 0
    discarded: Counter = field(default_factory=Counter)
    meters: dict[str, MeterImport] = field(default_factory=dict)


def fmt_ts(ts: datetime, tz: tzinfo | None = None) -> str:
    return (ts.astimezone(tz) if tz else ts).strftime("%d.%m.%Y %H:%M")


def fmt_num(v: float | None) -> str:
    if v is None:
        return "–"
    return f"{v:g}" if isinstance(v, float) else str(v)


def parse_line(line: str, tz: tzinfo) -> tuple[datetime, str, float | None] | None:
    """Zeile zerlegen: (Empfangszeit, Rohzeile, RSSI) oder None."""
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    try:
        received = datetime.strptime(m["ts"], "%Y.%m.%d %H:%M:%S").replace(tzinfo=tz)
    except ValueError:
        return None
    rssi = float(m["rssi"]) if m["rssi"] is not None else None
    return received, m["raw"], rssi


def _due_crossed(prev: Reading, cur: Reading) -> bool:
    """Liegt zwischen zwei Ablesungen ein Stichtag (31.12. bzw. Stichtag aus dem Telegramm)?"""
    due, _ = cur.due
    candidates = [date(y, 12, 31) for y in range(prev.ts.year, cur.ts.year + 1)]
    if due:
        candidates.append(due)
    a, b = prev.ts.date(), cur.ts.date()
    return any(a <= d < b for d in candidates)


def analyze(lines: Iterable[str], keys: dict[str, bytes | None], tz: tzinfo) -> Analysis:
    """Log auswerten. keys: angelegte Zähler-IDs -> AES-Schlüssel (oder None).

    Von nicht angelegten Zählern werden nur die Kopfdaten gelesen und nichts
    behalten außer dem Zähler für "verworfen".
    """
    res = Analysis()
    aes = {k: v for k, v in keys.items() if v}
    raw: dict[str, list[Reading]] = {}
    for line in lines:
        if not line.strip():
            continue
        res.lines += 1
        parsed = parse_line(line, tz)
        if parsed is None:
            res.discarded[R_FORMAT] += 1
            continue
        received, rawline, rssi = parsed
        try:
            frame, _ = wmbus.parse_cul_line(rawline)
        except wmbus.WMBusError:
            res.discarded[R_FRAME] += 1
            continue
        try:
            head = wmbus.decode(frame, header_only=True)
        except wmbus.WMBusError:
            res.discarded[R_DECODE] += 1
            continue
        if head.meter_id not in keys:
            res.discarded[R_FOREIGN] += 1
            continue
        try:
            tele = wmbus.decode(frame, aes)
        except wmbus.WMBusError as err:
            res.discarded[R_ENCRYPTED if "Schlüssel" in str(err) or "CRC" in str(err) else R_DECODE] += 1
            continue
        if tele.meter_id not in keys:
            res.discarded[R_FOREIGN] += 1
            continue
        if tele.encrypted and not tele.decrypted:
            res.discarded[R_ENCRYPTED] += 1
            continue
        values = tele.values()
        key = next((k for k in MAIN_KEYS if isinstance(values.get(k), (int, float))), None)
        if key is None:
            res.discarded[R_NOVALUE] += 1
            continue
        tele.rssi = rssi
        meter_time = values.get("datetime")
        if isinstance(meter_time, datetime):
            ts = meter_time if meter_time.tzinfo else meter_time.replace(tzinfo=tz)
        else:
            ts = received
        res.decoded += 1
        raw.setdefault(tele.meter_id, []).append(Reading(
            meter_id=tele.meter_id, ts=ts, received=received, from_meter=isinstance(meter_time, datetime),
            key=key, value=values[key], values=values, tele=tele, rssi=rssi,
        ))

    for mid, readings in raw.items():
        readings.sort(key=lambda r: (r.ts, r.received))
        first = readings[0]
        meter = MeterImport(mid, first.tele.type_name, first.tele.manufacturer, first.key)
        prev: Reading | None = None
        for r in readings:
            if r.key != meter.key:
                res.discarded[R_NOVALUE] += 1
                continue
            if prev is not None and r.ts == prev.ts and r.value == prev.value:
                res.discarded[R_DUPLICATE] += 1
                continue
            if prev is not None and r.value < prev.value:
                if meter.key == "hca" and _due_crossed(prev, r):
                    r.reset = True
                    meter.notes.append(
                        f"Stichtags-Reset zwischen {fmt_ts(prev.ts, tz)} und {fmt_ts(r.ts, tz)}: "
                        f"{fmt_num(prev.value)} → {fmt_num(r.value)}"
                    )
                else:
                    meter.anomalies.append(
                        f"Rückläufiger Stand {fmt_ts(r.ts, tz)}: {fmt_num(prev.value)} → {fmt_num(r.value)} "
                        "(nicht übernommen)"
                    )
                    res.discarded[R_BACKWARDS] += 1
                    continue
            meter.readings.append(r)
            prev = r
        if not any(r.from_meter for r in meter.readings):
            meter.notes.append("Keine Ablesezeit im Telegramm – Empfangszeit verwendet")
        res.meters[mid] = meter
    return res


# ======================================================================= Statistik


@dataclass
class Row:
    """Stündlicher Statistik-Zeitpunkt (Beginn der Stunde, UTC)."""

    start: datetime
    state: float
    sum: float | None = None
    day: date | None = None             # lokales Datum der Ablesung
    due_date: date | None = None
    due_value: float | None = None
    existing: bool = False


def hour_start(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def daily_rows(readings: list[Reading], tz: tzinfo) -> list[Row]:
    """Pro Tag der letzte Stand, Startzeitpunkt = Beginn seiner Stunde."""
    last: dict[date, Reading] = {}
    for r in readings:
        last[r.ts.astimezone(tz).date()] = r
    rows = []
    for day in sorted(last):
        r = last[day]
        due_date, due_value = r.due
        rows.append(Row(hour_start(r.ts), float(r.value), day=day, due_date=due_date, due_value=due_value))
    return rows


def consumption(prev: Row, cur: Row, hca: bool) -> float:
    """Verbrauch zwischen zwei Ständen (Summenzuwachs)."""
    if cur.state >= prev.state:
        return cur.state - prev.state
    if hca:
        # Rücksprung = Stichtags-Reset: Verbrauch bis zum Stichtag + Stand danach
        pday = prev.day or prev.start.date()
        cday = cur.day or cur.start.date()
        if cur.due_value is not None and cur.due_date and pday <= cur.due_date < cday:
            return max(0.0, cur.due_value - prev.state) + cur.state
        return cur.state
    return 0.0          # rückläufig (sollte vorher aussortiert sein): nichts zählen


@dataclass
class StatPlan:
    rows: list[Row] = field(default_factory=list)                  # zu importieren, mit Summe
    adjustments: list[tuple[datetime, float]] = field(default_factory=list)
    skipped_existing: int = 0           # Stunde schon vorhanden (ohne overwrite)
    skipped_newer: int = 0              # nicht älter als die jüngste vorhandene Statistik


def plan_statistics(
    rows: list[Row],
    existing: list[Row],
    short_term: Row | None,
    current_state: float | None,
    *,
    hca: bool,
    overwrite: bool = False,
) -> StatPlan:
    """Importierte Tageswerte nahtlos in vorhandene Statistiken einfügen.

    - existing: vorhandene Stundenwerte (state, sum) des Sensors.
    - short_term: jüngster 5-Minuten-Wert; daran setzt die Live-Statistik an.
    - current_state: aktueller Zustand des Sensors (für den Anschluss, wenn es
      noch gar keine Statistik gibt; der erste Live-Wert beginnt dann mit Summe 0).

    Vor vorhandenen Werten wird rückwärts vom ersten vorhandenen Wert aus
    gerechnet (dessen Summe bleibt). In Lücken wird vorwärts gerechnet; passt die
    Summe des nächsten vorhandenen Werts nicht, wird er samt allen späteren
    Werten verschoben (adjustments). Neuere Werte als die jüngste vorhandene
    Statistik werden nicht importiert – den Anschluss an die Gegenwart übernimmt
    die Live-Statistik.
    """
    plan = StatPlan()
    existing = sorted(existing, key=lambda r: r.start)
    ex_starts = {r.start for r in existing}
    anchor: Row | None = None
    keep: list[Row] = []
    if existing:
        newest = existing[-1].start
        for r in rows:
            if r.start >= newest:
                plan.skipped_newer += 1
            elif r.start in ex_starts and not overwrite:
                plan.skipped_existing += 1
            else:
                keep.append(r)
    else:
        limit = short_term.start.replace(minute=0) if short_term else None
        for r in rows:
            if limit is not None and r.start >= limit:
                plan.skipped_newer += 1
            else:
                keep.append(r)
        if keep:
            if short_term is not None:
                anchor = Row(short_term.start, short_term.state, short_term.sum or 0.0, existing=True)
            else:
                state = current_state if current_state is not None else keep[-1].state
                anchor = Row(keep[-1].start + timedelta(hours=1), state, 0.0, existing=True)
    if not keep:
        return plan

    replaced = {r.start for r in keep}
    timeline = sorted(
        [Row(e.start, e.state, e.sum or 0.0, existing=True) for e in existing if e.start not in replaced] + keep,
        key=lambda r: r.start,
    )
    shift = 0.0
    i = 0
    while i < len(timeline):
        if timeline[i].existing:
            timeline[i].sum += shift
            i += 1
            continue
        j = i
        while j < len(timeline) and not timeline[j].existing:
            j += 1
        run = timeline[i:j]
        prev = timeline[i - 1] if i > 0 else None
        follower = timeline[j] if j < len(timeline) else anchor
        if prev is None:
            # rückwärts vom ersten vorhandenen Wert (bzw. dem Live-Startpunkt)
            nxt = follower
            nxt_sum = nxt.sum + (shift if nxt is not anchor else 0.0)
            for r in reversed(run):
                r.sum = nxt_sum - consumption(r, nxt, hca)
                nxt, nxt_sum = r, r.sum
        else:
            s, p = prev.sum, prev
            for r in run:
                s += consumption(p, r, hca)
                r.sum, p = s, r
            if follower is not None and follower is not anchor:
                needed = s + consumption(p, follower, hca)
                adj = needed - (follower.sum + shift)
                if abs(adj) > 1e-9:
                    plan.adjustments.append((follower.start, round(adj, 9)))
                    shift += adj
        for r in run:
            r.sum = round(r.sum, 9)
        plan.rows.extend(run)
        i = j
    return plan
