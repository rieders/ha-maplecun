# SPDX-License-Identifier: GPL-2.0-only
"""FHEM-Log importieren: aktueller Zustand und Langzeitstatistik.

Gemeinsame Logik für die Aktion maplecun.import_fhem_log und den Options-Flow.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from . import fhemlog
from .const import CONF_METERS, CONF_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def read_and_analyze(path: str | Path, keys: dict[str, bytes | None], tz) -> fhemlog.Analysis:
    """Datei lesen und auswerten (blockierend, im Executor aufrufen)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fhemlog.analyze(fh, keys, tz)


async def async_analyze_file(hass: HomeAssistant, hub, path: str | Path) -> fhemlog.Analysis:
    tz = dt_util.get_default_time_zone()
    return await hass.async_add_executor_job(read_and_analyze, path, dict(hub.meters), tz)


# ----------------------------------------------------------------------- Recorder


def _recorder_ready(hass: HomeAssistant) -> bool:
    return "recorder" in hass.config.components


async def _async_existing(hass: HomeAssistant, statistic_id: str):
    """Vorhandene Stundenwerte, jüngster 5-Minuten-Wert und Metadaten."""
    from homeassistant.components.recorder import get_instance, statistics

    def _load():
        hours = statistics.statistics_during_period(
            hass, _EPOCH, None, {statistic_id}, "hour", None, {"state", "sum"}
        ).get(statistic_id, [])
        short = statistics.get_last_short_term_statistics(
            hass, 1, statistic_id, False, {"state", "sum"}
        ).get(statistic_id, [])
        meta = statistics.get_metadata(hass, statistic_ids={statistic_id}).get(statistic_id)
        return hours, short, meta

    hours, short, meta = await get_instance(hass).async_add_executor_job(_load)

    def _row(s) -> fhemlog.Row:
        start = s["start"]
        if not isinstance(start, datetime):
            start = dt_util.utc_from_timestamp(start)
        return fhemlog.Row(start, float(s.get("state") or 0.0), float(s.get("sum") or 0.0), existing=True)

    return (
        [_row(s) for s in hours],
        _row(short[0]) if short else None,
        meta[1] if meta else None,
    )


def _write_statistics(
    hass: HomeAssistant, statistic_id: str, unit: str | None, unit_class: str | None, plan: fhemlog.StatPlan
) -> None:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics

    instance = get_instance(hass)
    # zuerst vorhandene Summen verschieben, dann einfügen (Recorder-Warteschlange ist geordnet)
    for start, adj in plan.adjustments:
        instance.async_adjust_statistics(statistic_id, start, adj, unit)
    if plan.rows:
        async_import_statistics(
            hass,
            {
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
                "name": None,
                "source": "recorder",
                "statistic_id": statistic_id,
                "unit_of_measurement": unit,
                "unit_class": unit_class,
            },
            [{"start": r.start, "state": r.state, "sum": r.sum} for r in plan.rows],
        )


# ----------------------------------------------------------------------- Import


def _float_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    st = hass.states.get(entity_id) if entity_id else None
    try:
        return float(st.state) if st else None
    except (TypeError, ValueError):
        return None


async def _async_wait_entity(ent_reg: er.EntityRegistry, unique_id: str) -> str | None:
    for _ in range(50):
        if eid := ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id):
            return eid
        await asyncio.sleep(0.1)
    return None


async def async_import(
    hass: HomeAssistant, entry, analysis: fhemlog.Analysis, *, dry_run: bool, overwrite: bool
) -> dict[str, Any]:
    """Auswertung anwenden (oder bei dry_run nur berichten)."""
    hub = entry.runtime_data
    tz = dt_util.get_default_time_zone()
    ent_reg = er.async_get(hass)
    names = {mid: cfg.get(CONF_NAME) for mid, cfg in entry.options.get(CONF_METERS, {}).items()}
    report: dict[str, Any] = {
        "probelauf": dry_run,
        "zeilen_gesamt": analysis.lines,
        "dekodiert": analysis.decoded,
        "verworfen": dict(analysis.discarded),
        "verworfen_gesamt": sum(analysis.discarded.values()),
        "zaehler": [],
    }
    for mid in sorted(analysis.meters):
        m = analysis.meters[mid]
        info: dict[str, Any] = {
            "id": mid,
            "name": names.get(mid) or f"{m.type_name} {mid}",
            "typ": m.type_name,
            "werte": len(m.readings),
            "auffaelligkeiten": list(m.anomalies),
            "hinweise": list(m.notes),
        }
        report["zaehler"].append(info)
        if not m.readings:
            continue
        first, latest = m.readings[0], m.readings[-1]
        info.update(
            von=first.ts.astimezone(tz).isoformat(timespec="minutes"),
            bis=latest.ts.astimezone(tz).isoformat(timespec="minutes"),
            erster_stand=first.value,
            letzter_stand=latest.value,
        )

        # 1) aktueller Zustand – nur wenn neuer als der vorhandene
        last_seen = hub.seen.get(mid, {}).get("last_seen")
        newer = last_seen is None or latest.received > datetime.fromisoformat(last_seen)
        info["zustand"] = (
            ("wird gesetzt" if dry_run else "gesetzt") if newer
            else f"unverändert (vorhandener Wert vom {fhemlog.fmt_ts(datetime.fromisoformat(last_seen), tz)} ist neuer)"
        )
        unique_id = f"{entry.entry_id}_meter_{mid}_{m.key}"
        entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
        if newer and not dry_run:
            hub.deliver(latest.tele, latest.rssi, latest.received.astimezone(timezone.utc), None,
                        line=latest.line, lqi=latest.lqi)
            if entity_id is None:
                entity_id = await _async_wait_entity(ent_reg, unique_id)
            # Zustand muss stehen, bevor der Anschluss der Statistik berechnet wird
            for _ in range(20):
                if _float_state(hass, entity_id) == float(latest.value):
                    break
                await asyncio.sleep(0.05)

        # 2) Verlauf (Langzeitstatistik)
        info["statistik"] = await _async_statistics(
            hass, m, entity_id, latest if newer else None, tz, dry_run=dry_run, overwrite=overwrite
        )

    report["text"] = format_report(report)
    title = "MapleCUN: FHEM-Import" + (" (Probelauf)" if dry_run else "")
    persistent_notification.async_create(
        hass, report["text"], title=title, notification_id=f"{DOMAIN}_{entry.entry_id}_fhem_import"
    )
    if not dry_run:
        hub._save_later(1)
    return report


async def _async_statistics(
    hass: HomeAssistant, m: fhemlog.MeterImport, entity_id: str | None, latest_applied,
    tz, *, dry_run: bool, overwrite: bool,
) -> dict[str, Any]:
    rows = fhemlog.daily_rows(m.readings, tz)
    out: dict[str, Any] = {"tage": len(rows), "statistic_id": entity_id}
    if not _recorder_ready(hass):
        out["status"] = "Recorder nicht aktiv – keine Statistik"
        return out
    unit = fhemlog.UNITS.get(m.key)
    if entity_id is None:
        out["status"] = "Sensor existiert noch nicht" + (" – wird beim Import angelegt" if dry_run else "")
        existing, short, meta = [], None, None
    else:
        existing, short, meta = await _async_existing(hass, entity_id)
        if meta and meta.get("unit_of_measurement") != unit:
            out["status"] = (
                f"übersprungen: vorhandene Statistik in {meta.get('unit_of_measurement')}, Import in {unit}"
            )
            return out
    if latest_applied is not None:
        current = float(latest_applied.value)
    else:
        current = _float_state(hass, entity_id)
    plan = fhemlog.plan_statistics(rows, existing, short, current, hca=m.key == "hca", overwrite=overwrite)
    out.update(
        neue_stunden=len(plan.rows),
        vorhanden_uebersprungen=plan.skipped_existing,
        neuer_als_vorhandene=plan.skipped_newer,
        summen_angepasst=len(plan.adjustments),
    )
    if plan.rows:
        out["erste_summe"] = plan.rows[0].sum
        out["letzte_summe"] = plan.rows[-1].sum
    if not dry_run and entity_id is not None:
        unit_class = meta.get("unit_class") if meta else fhemlog.UNIT_CLASSES.get(m.key)
        _write_statistics(hass, entity_id, unit, unit_class, plan)
        out["status"] = "importiert"
    else:
        out.setdefault("status", "Probelauf")
    return out


# ----------------------------------------------------------------------- Bericht


def format_report(r: dict[str, Any]) -> str:
    lines = [
        f"**{'Probelauf – nichts geschrieben' if r['probelauf'] else 'Import ausgeführt'}**",
        "",
        f"Zeilen gesamt: {r['zeilen_gesamt']} · dekodiert: {r['dekodiert']} · verworfen: {r['verworfen_gesamt']}",
    ]
    for reason, n in sorted(r["verworfen"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- {reason}: {n}")
    if not r["zaehler"]:
        lines += ["", "Keine Telegramme angelegter Zähler gefunden."]
    for z in r["zaehler"]:
        lines += ["", f"**{z['name']}** ({z['id']}, {z['typ']}): {z['werte']} Werte"]
        if z["werte"]:
            lines.append(
                f"- Zeitraum: {_d(z['von'])} – {_d(z['bis'])}; Stand {fhemlog.fmt_num(z['erster_stand'])} → "
                f"{fhemlog.fmt_num(z['letzter_stand'])}"
            )
            lines.append(f"- Aktueller Wert: {z.get('zustand')}")
            st = z.get("statistik", {})
            if "neue_stunden" in st:
                lines.append(
                    f"- Statistik: {st['neue_stunden']} Tageswerte"
                    + (f", {st['vorhanden_uebersprungen']} schon vorhanden" if st["vorhanden_uebersprungen"] else "")
                    + (f", {st['neuer_als_vorhandene']} neuer als die vorhandene Statistik"
                       if st["neuer_als_vorhandene"] else "")
                    + (f", vorhandene Summen {st['summen_angepasst']}× angepasst" if st["summen_angepasst"] else "")
                    + (f" ({st['status']})" if st.get("status") else "")
                )
            elif st.get("status"):
                lines.append(f"- Statistik: {st['status']}")
        for a in z["auffaelligkeiten"]:
            lines.append(f"- ⚠️ {a}")
        for n in z["hinweise"]:
            lines.append(f"- ℹ️ {n}")
    return "\n".join(lines)


def _d(iso: str) -> str:
    return fhemlog.fmt_ts(datetime.fromisoformat(iso))
