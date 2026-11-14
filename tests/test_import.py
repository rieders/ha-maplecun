"""FHEM-Log-Import: Aktion, Zustand, Langzeitstatistik, Options-Flow."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.maplecun.const import DOMAIN

from .fhem_sim import REAL, hca, log_line, water
from .test_maplecun import REPEATED, _modules, _settle, firmware, mods  # noqa: F401

EID = "sensor.wasser_52143909_meter_reading"
FILE_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
FOREIGN = f"2026.11.12 07:41:10 3: WMBUS Unknown device {REPEATED[:-2]}::-70.5, please define it"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Recorder muss vor hass aufgesetzt werden."""
    yield


@pytest.fixture
async def setup(hass: HomeAssistant, mods, tmp_path):
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    await hass.config.async_set_time_zone("Europe/Berlin")
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="MapleCUN test", unique_id="127.0.0.1",
        data={"host": "127.0.0.1", "modules": _modules(mods)},
        options={"init_mode": False, "revolts": {}, "it_devices": {},
                 "meters": {"52143909": {"name": "", "key": ""}, "16660259": {"name": "HKV Bad", "key": ""}}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass, 0.5)
    return entry


def _write(tmp_path: Path, lines: list[str]) -> str:
    p = tmp_path / "meine_zaehler.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


async def _call(hass, path, **kw):
    return await hass.services.async_call(
        DOMAIN, "import_fhem_log", {"path": path, **kw}, blocking=True, return_response=True)


async def _stats(hass, eid=EID):
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=timezone.utc), None, {eid}, "hour",
        None, {"state", "sum"})
    return [(dt_util.utc_from_timestamp(r["start"]), r["state"], round(r["sum"], 6)) for r in rows.get(eid, [])]


def _water_series(days: int, start=datetime(2026, 10, 1, 6, 0), liters=50000, step=100):
    return [log_line(start + timedelta(days=d, minutes=3), water("52143909", liters + d * step, start + timedelta(days=d)))
            for d in range(days)]


# ------------------------------------------------------------------ Aktion
async def test_dry_run_real_line(hass: HomeAssistant, setup, tmp_path):
    entry = setup
    path = _write(tmp_path, [REAL, FOREIGN, "Müll"])
    r = await _call(hass, path)
    assert r["probelauf"] is True
    assert (r["zeilen_gesamt"], r["dekodiert"], r["verworfen_gesamt"]) == (3, 1, 2)
    assert r["verworfen"] == {"fremder Zähler (nicht angelegt)": 1, "Zeilenformat nicht erkannt": 1}
    (z,) = r["zaehler"]
    assert (z["id"], z["typ"], z["werte"], z["erster_stand"], z["letzter_stand"]) == (
        "52143909", "Wasser", 1, 56.178, 56.178)
    assert z["von"] == "2026-11-12T04:20+01:00"                    # Ablesezeit aus dem Telegramm
    # nichts geschrieben
    hub = entry.runtime_data
    assert hub.seen == {} and hub.meter_keys == {} and hass.states.get(EID) is None
    assert await _stats(hass) == []
    # Bericht auch als Benachrichtigung; fremder Zähler nirgends
    from homeassistant.components import persistent_notification as pn
    note = pn._async_get_or_create_notifications(hass)[f"{DOMAIN}_{entry.entry_id}_fhem_import"]
    assert "Probelauf" in note["message"] and "52143909" in note["message"]
    assert "52143906" not in note["message"] and "52143906" not in str(r)


async def test_path_not_allowed(hass: HomeAssistant, setup, tmp_path):
    with pytest.raises(ServiceValidationError):
        await _call(hass, "/etc/passwd")


async def test_import_real_line_sets_state(hass: HomeAssistant, setup, tmp_path, hass_storage):
    entry = setup
    path = _write(tmp_path, [REAL, FOREIGN])
    r = await _call(hass, path, dry_run=False)
    await _settle(hass, 0.3)
    st = hass.states.get(EID)
    assert float(st.state) == 56.178 and st.attributes["state_class"] == "total_increasing"
    assert float(hass.states.get("sensor.wasser_52143909_due_date_value").state) == 17.965
    assert hass.states.get("sensor.wasser_52143909_due_date").state == "2025-12-31"
    assert hass.states.get("sensor.wasser_52143909_month_end_value").state == "54.915"
    last = dt_util.parse_datetime(hass.states.get("sensor.wasser_52143909_last_received").state)
    assert last == datetime(2026, 11, 12, 7, 40, 35, tzinfo=dt_util.get_default_time_zone())
    hub = entry.runtime_data
    assert set(hub.seen) == {"52143909"} and set(hub.meter_keys) == {"52143909"}
    assert hub.heard == {}                                      # Import ist kein Empfang seit dem Start
    assert r["zaehler"][0]["zustand"] == "gesetzt"
    # Rohzeile über denselben Weg wie beim Live-Empfang (hub.deliver)
    raw = hass.states.get("sensor.wasser_52143909_last_telegram_raw")
    assert raw.state.startswith("b4BC465B2769") and raw.attributes["line"].endswith("9FE880")
    assert raw.attributes["netzwerkknoten"] is True and raw.attributes["lqi"] == 0x80
    assert raw.attributes["rssi"] == -78.5 and raw.attributes["inneres_telegramm"]
    assert raw.attributes["empfangen"].startswith("2026-11-12T07:40:35")
    rows = await _stats(hass)
    assert rows == [(datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc), 56.178, 0.0)]
    # übersteht ein Neuladen (gleicher Codepfad wie Live-Empfang)
    await hass.config_entries.async_reload(entry.entry_id)
    await _settle(hass, 0.5)
    assert float(hass.states.get(EID).state) == 56.178
    assert hass.states.get("sensor.wasser_52143909_last_telegram_raw").state.startswith("b4BC465B2769")
    assert hass.states.get("sensor.wasser_52143909_last_received").state not in ("unknown", "unavailable")
    # fremder Zähler nirgends gespeichert (Laufzeitdaten, Speicherdatei, Optionen, Statistik)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "52143906" not in str(hass_storage.get(f"{DOMAIN}.{entry.entry_id}"))
    assert "52143906" not in str(dict(entry.options))
    stat_ids = await get_instance(hass).async_add_executor_job(list_statistic_ids, hass)
    assert not any("52143906" in s["statistic_id"] for s in stat_ids)


async def test_series_statistics_and_newer_live_value(hass: HomeAssistant, setup, tmp_path, mods):
    entry = setup
    # Live-Telegramm (neuer als der Import) zuerst
    live = water("52143909", 50900, datetime(2026, 11, 20, 6, 0))
    from .fhem_sim import cul_line
    await mods["1"].send(cul_line(live))
    await _settle(hass, 0.5)
    assert float(hass.states.get(EID).state) == 50.9
    path = _write(tmp_path, _water_series(5))                 # 01.–05.10.: 50.0 … 50.4
    r = await _call(hass, path, dry_run=False)
    await _settle(hass, 0.3)
    assert float(hass.states.get(EID).state) == 50.9          # älterer Import überschreibt nicht
    assert r["zaehler"][0]["zustand"].startswith("unverändert")
    rows = await _stats(hass)
    assert [s for _, s, _ in rows] == [50.0, 50.1, 50.2, 50.3, 50.4]
    # Anschluss an den Live-Startpunkt (Summe 0 bei 50.9): 50.4 -> 50.9 = 0.5
    assert [u for *_, u in rows] == [-0.9, -0.8, -0.7, -0.6, -0.5]
    assert rows[0][0] == datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)   # 06:00 MESZ, stündlich


async def test_seamless_join_existing_statistics(hass: HomeAssistant, setup, tmp_path, mods):
    """Vorhandene (Live-)Statistik ab 10.10.: Import davor schließt ohne Sprung an, Lücke wird gefüllt."""
    from .fhem_sim import cul_line
    await mods["1"].send(cul_line(water("52143909", 51700, datetime(2026, 10, 17, 6, 0))))   # legt den Sensor an
    await _settle(hass, 0.5)
    assert float(hass.states.get(EID).state) == 51.7
    meta = {"has_sum": True, "mean_type": StatisticMeanType.NONE, "name": None, "source": "recorder",
            "statistic_id": EID, "unit_of_measurement": "m³", "unit_class": "volume"}
    t = lambda d, h: datetime(2026, 10, d, h, tzinfo=timezone.utc)  # noqa: E731
    existing = [(t(10, 4), 50.9, 0.0), (t(10, 5), 50.9, 0.0), (t(15, 4), 51.5, 0.3), (t(16, 4), 51.6, 0.4)]
    # 15.10.: HA hat nach einer Lücke nur 0.3 statt 0.6 gezählt (z. B. Reset erkannt)
    async_import_statistics(hass, meta, [{"start": s, "state": x, "sum": u} for s, x, u in existing])
    await async_wait_recording_done(hass)
    before = await _stats(hass)

    lines = _water_series(12)                                  # 01.–12.10. 06:00: 50.0 … 51.1
    path = _write(tmp_path, lines)
    r = await _call(hass, path, dry_run=False)
    st = r["zaehler"][0]["statistik"]
    rows = await _stats(hass)
    by = {s: (x, u) for s, x, u in rows}
    # Präfix 01.–09.10.: rückwärts von (10.10., 50.9, 0.0) – keine Stufe am Übergang
    assert by[t(9, 4)] == (50.8, -0.1) and by[t(1, 4)] == (50.0, -0.9)
    # 10.10. 04:00 schon vorhanden -> nicht überschrieben
    assert by[t(10, 4)] == (50.9, 0.0) and st["vorhanden_uebersprungen"] == 1
    # Lücke 11.–12.10. gefüllt, Summe des 15.10. auf 0.6 angehoben, spätere mit
    assert by[t(11, 4)] == (51.0, 0.1) and by[t(12, 4)] == (51.1, 0.2)
    assert by[t(15, 4)] == (51.5, 0.6) and by[t(16, 4)] == (51.6, 0.7)
    assert st["summen_angepasst"] == 1
    # keine negativen Verbräuche zwischen aufeinanderfolgenden Stunden
    sums = [u for *_, u in rows]
    assert all(b >= a for a, b in zip(sums, sums[1:]))
    assert len(before) < len(rows)


async def test_hca_reset_statistics(hass: HomeAssistant, setup, tmp_path):
    due = date(2025, 12, 31)
    lines = [
        log_line(datetime(2025, 12, 30, 12), hca("16660259", 400, datetime(2025, 12, 30, 12), date(2024, 12, 31), 300)),
        log_line(datetime(2025, 12, 31, 12), hca("16660259", 430, datetime(2025, 12, 31, 12), date(2024, 12, 31), 300)),
        log_line(datetime(2026, 1, 2, 12), hca("16660259", 20, datetime(2026, 1, 2, 12), due, 450)),
    ]
    r = await _call(hass, _write(tmp_path, lines), dry_run=False)
    await _settle(hass, 0.3)
    z = r["zaehler"][0]
    assert z["name"] == "HKV Bad" and z["auffaelligkeiten"] == [] and "Stichtags-Reset" in z["hinweise"][0]
    eid = "sensor.hkv_bad_consumption_units"
    assert hass.states.get(eid).state == "20"
    rows = await _stats(hass, eid)
    # 400->430: 30; 430->450 (Stichtagswert): 20; Reset, danach 20 -> zusammen 70
    assert [(x, u) for _, x, u in rows] == [(400, -70), (430, -40), (20, 0)]


# ------------------------------------------------------------------ Options-Flow
async def test_options_flow_upload(hass: HomeAssistant, setup, tmp_path):
    entry = setup
    path = Path(_write(tmp_path, [REAL, FOREIGN]))
    deleted = []

    @contextmanager
    def fake_upload(hass_, file_id):
        assert file_id == FILE_ID
        try:
            yield path
        finally:
            path.unlink()
            deleted.append(file_id)

    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert "import_fhem" in r["menu_options"]
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "import_fhem"})
    assert r["step_id"] == "import_fhem"
    with patch("homeassistant.components.file_upload.process_uploaded_file", fake_upload):
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"file": FILE_ID})
    assert deleted == [FILE_ID] and not path.exists()           # Datei sofort gelöscht
    assert r["type"] is FlowResultType.MENU and r["step_id"] == "import_report"
    assert r["menu_options"] == ["import_confirm", "import_cancel"]
    report = r["description_placeholders"]["report"]
    assert "Probelauf" in report and "56.178" in report and "fremder Zähler" in report
    assert hass.states.get(EID) is None                       # Probelauf schreibt nichts
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "import_confirm"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "import_done"
    await _settle(hass, 0.3)
    assert float(hass.states.get(EID).state) == 56.178

    # Abbrechen schreibt nichts
    path = Path(_write(tmp_path, _water_series(2)))
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "import_fhem"})
    with patch("homeassistant.components.file_upload.process_uploaded_file", fake_upload):
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"file": FILE_ID})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "import_cancel"})
    assert r["reason"] == "import_cancelled"
    assert float(hass.states.get(EID).state) == 56.178
