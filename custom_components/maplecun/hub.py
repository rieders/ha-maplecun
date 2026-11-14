"""Verbindungen zu den Funkmodulen des MapleCUN, Geräteerkennung und Datenverteilung."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import re
import time
from dataclasses import dataclass, field

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import climate433, culcmd, intertechno, revolt, wmbus
from .const import (
    CONF_AUTO,
    CONF_INIT,
    CONF_IT_DEVICES,
    CONF_KEY,
    CONF_LOG_UNKNOWN,
    CONF_METERS,
    CONF_MODULES,
    CONF_NAME,
    CONF_OFF,
    CONF_ON,
    CONF_MODEL,
    CONF_REVOLTS,
    CONF_ROLE,
    CONF_SENSORS,
    DEFAULT_AUTO,
    DEFAULT_IT_OFF,
    DEFAULT_IT_ON,
    DOMAIN,
    KIND_IT,
    KIND_METER,
    KIND_REVOLT,
    KIND_SENSOR,
    CONFLICT_WMBUS,
    MODE_REPLY,
    MODE_REPLY_RE,
    MODE_RETRY_DELAY,
    RECONNECT_DELAY,
    REPLY_TIMEOUT,
    ROLE_MODE_CMD,
    ROLE_MONITOR,
    ROLE_NEEDS,
    ROLE_OFF,
    HEARD_MAX,
    INFO_SLOW_INTERVAL,
    IT_REINIT_DELAY,
    STARTUP_SLOWRF_WAIT,
    WATCHDOG_SILENCE_SLOWRF,
    WMBUS_OFF_CMD,
    ROLE_RF433,
    ROLE_WMBUS_C,
    SEEN_MAX,
    STORAGE_VERSION,
    UNSUPPORTED_NO_B,
    UNSUPPORTED_NO_CMODE,
    UNSUPPORTED_UNKNOWN,
    WATCHDOG_CHECK,
    WATCHDOG_REPEAT,
    WATCHDOG_SILENCE,
    WMBUS_ROLES,
    signal_climate,
    signal_connection,
    signal_discovered,
    signal_it,
    signal_meter,
    signal_meter_new,
    signal_meter_raw,
    signal_module_info,
    signal_raw,
    signal_rx,
    signal_revolt,
    signal_seen,
)

_LOGGER = logging.getLogger(__name__)


def parse_cmds(reply: str) -> str:
    """Antwort auf "?" -> Kommandobuchstaben.

    a-culfw (clib/multi_CC.c, callfn2): "? (? is unknown) Use one of B b C F … *".
    """
    tail = reply.split("Use one of", 1)[-1]
    return "".join(tok for tok in tail.split() if len(tok) == 1)


@dataclass
class Module:
    """Ein CC1101-Funkmodul (= ein Port am Split-Proxy)."""

    module_id: str
    port: int
    role: str
    connected: bool = False
    last_rx: object = None            # datetime des letzten Telegramms
    rx_count: int = 0                 # Zeilen seit Mitternacht
    rx_day: int = 0
    rx_notified: float = 0.0
    mode_retry: float = -1e9          # letztes erneutes Setzen nach "OFF" (monotonic)
    last_line: float = 0.0            # monotonic der letzten Zeile (oder des Verbindens)
    watchdog_at: float = -1e9         # letztes Neusetzen durch den Wächter (monotonic)
    # SlowRF (433 MHz): zuletzt mit "X21" initialisiert, und ob wMBus dabei sicher aus war
    radio_at: object = None
    radio_ok: bool = False
    # Firmware
    version: str | None = None        # Antwort auf "V"
    cmds: str | None = None           # Antwort auf "?", nur die Buchstaben, z. B. "bCFiAZ…"
    mode_reply: str | None = None     # letzte Antwort auf den Empfangsmodus-Befehl
    mode_cmd: str | None = None
    mode_time: object = None
    unsupported: str | None = None    # UNSUPPORTED_* – Rolle wird nicht (mehr) gesetzt
    conflict: str | None = None       # Modul-ID, die den wMBus-Empfang belegt
    skipped: bool = False             # Modus wegen Konflikt nicht gesetzt -> nachholen, wenn frei
    info: dict = field(default_factory=dict)        # Modulwerte (Aufgabe 3)
    info_at: dict = field(default_factory=dict)     # Abrufzeitpunkte (monotonic)
    waiters: list = field(default_factory=list)     # (Regex, Future) für Antworten
    writer: asyncio.StreamWriter | None = None
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    req_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def is_wmbus(self) -> bool:
        return self.role in WMBUS_ROLES


class MapleCunHub:
    """Verwaltet alle Modul-Verbindungen eines MapleCUN."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id: str = entry.entry_id
        self.host: str = entry.data["host"]
        self.device_id: str | None = None        # Geräteregistrierung (MapleCUN), setzt __init__
        opts = entry.options
        self.init_mode: bool = opts.get(CONF_INIT, True)
        self.auto: dict[str, bool] = {**DEFAULT_AUTO, **opts.get(CONF_AUTO, {})}
        self.log_unknown: bool = opts.get(CONF_LOG_UNKNOWN, False)
        self.modules: dict[str, Module] = {
            mid: Module(mid, int(cfg["port"]), cfg[CONF_ROLE])
            for mid, cfg in entry.data[CONF_MODULES].items()
            if cfg[CONF_ROLE] != ROLE_OFF
        }
        # Eingerichtete Geräte
        self.meters: dict[str, bytes | None] = {
            mid: (bytes.fromhex(cfg[CONF_KEY]) if cfg.get(CONF_KEY) else None)
            for mid, cfg in opts.get(CONF_METERS, {}).items()
        }
        self.revolts: set[str] = set(opts.get(CONF_REVOLTS, {}))
        self.it_codes: set[str] = set(opts.get(CONF_IT_DEVICES, {}))
        self.climate: set[str] = set(opts.get(CONF_SENSORS, {}))

        # Laufzeitdaten (gespeichert)
        self.meter_keys: dict[str, list[str]] = {}
        self.seen: dict[str, dict] = {}          # eingerichtete Zähler: Hersteller, Typ, RSSI …
        self.discovered: dict[str, dict] = {}    # "kind:id" -> Info, noch nicht übernommen
        self.ignored: set[str] = set()
        self.legacy_revolts: list[str] = []      # aus Version 0.1/0.2 (Speicher)
        # Seit dem Start gehörte Zähler (eigene und fremde), nur Kopfdaten, nur im
        # Arbeitsspeicher – wird nie gespeichert und beginnt nach einem Neustart leer
        self.heard: dict[str, dict] = {}
        # Letzte Rohzeile je ANGELEGTEM Zähler (nur Arbeitsspeicher; der Sensor stellt sie wieder her)
        self.meter_raw: dict[str, dict] = {}

        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{self.entry_id}")
        self._last_energy: dict[str, tuple[float, float]] = {}
        self._adopting: set[str] = set()
        self._notified = 0
        self._modes: dict[str, dict] = {}
        self._unsub_watchdog = None
        # Initialisierung der Funkmodule: eine gemeinsame Sperre für alle Module, weil a-culfw
        # den wMBus-Zustand global führt (SlowRF zuerst, wMBus zuletzt – siehe const.py)
        self._radio_lock = asyncio.Lock()
        self._wmbus_on: bool | None = None       # None = unbekannt (z. B. vom letzten Lauf)
        self._wmbus_started = False              # wMBus-Modul hat seine erste Initialisierung hinter sich
        self._slowrf_ready = asyncio.Event()     # alle SlowRF-Module einmal initialisiert (Start)
        self._owner_known = asyncio.Event()      # wMBus-Modul hat V/? beantwortet (Start)
        self._it_reinit: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ Lebenszyklus
    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        self.legacy_revolts = list(data.get("revolts", []))
        self.meter_keys = {k: list(v) for k, v in data.get("meter_keys", {}).items() if k in self.meters}
        # Fremde Zähler (Nachbarn) werden nicht gespeichert – auch keine Altbestände
        self.seen = {k: v for k, v in data.get("seen", {}).items() if k in self.meters}
        self.ignored = {k for k in data.get("ignored", []) if not k.startswith(f"{KIND_METER}:")}
        self.discovered = {
            k: v for k, v in data.get("discovered", {}).items()
            if k not in self.ignored and not self.is_configured(k) and not k.startswith(f"{KIND_METER}:")
        }
        # Firmware-Befunde: "Rolle nicht unterstützt" gilt nur für dieselbe Rolle und Firmware
        self._modes = {k: dict(v) for k, v in data.get("modes", {}).items()}
        for mid, mod in self.modules.items():
            saved = self._modes.get(mid, {})
            mod.version = saved.get("version")
            mod.cmds = saved.get("cmds")              # Kommandoliste hängt nicht an der Rolle
            if saved.get("role") == mod.role:
                mod.unsupported = saved.get("unsupported")
            elif saved:
                saved.update(role=mod.role, unsupported=None)
        self._update_conflicts()
        self._check_slowrf_ready()
        if self.wmbus_owner() is None or not self.init_mode:
            self._owner_known.set()
        for mod in self.modules.values():
            mod.task = self.hass.async_create_background_task(
                self._run(mod), f"{DOMAIN}_{self.entry_id}_module_{mod.module_id}"
            )
        self._unsub_watchdog = async_track_time_interval(
            self.hass, self._async_watchdog, timedelta(seconds=WATCHDOG_CHECK)
        )
        self._update_notification()

    async def async_stop(self) -> None:
        if self._unsub_watchdog:
            self._unsub_watchdog()
            self._unsub_watchdog = None
        for task in self._it_reinit.values():
            task.cancel()
        for mod in self.modules.values():
            if mod.task:
                mod.task.cancel()
                try:
                    await mod.task
                except asyncio.CancelledError:
                    pass
                mod.task = None
            await self._close(mod)
        await self._store.async_save(self._store_data())

    def _store_data(self) -> dict:
        return {
            "meter_keys": self.meter_keys,
            "seen": self.seen,
            "discovered": self.discovered,
            "ignored": sorted(self.ignored),
            "modes": self._modes,
        }

    def _save_later(self, delay: float = 10) -> None:
        self._store.async_delay_save(self._store_data, delay)

    # ------------------------------------------------------------------ Verbindung
    def module_for_role(self, role: str) -> Module | None:
        return next((m for m in self.modules.values() if m.role == role), None)

    def module_cmds(self, module_id: str) -> str | None:
        """Kommandoliste eines Moduls ("?"), auch von gerade abgeschalteten Modulen (gespeichert)."""
        if (mod := self.modules.get(module_id)) is not None and mod.cmds:
            return mod.cmds
        return self._modes.get(module_id, {}).get("cmds")

    def is_connected(self, module_id: str | None) -> bool:
        mod = self.modules.get(module_id) if module_id else None
        return bool(mod and mod.connected)

    async def _close(self, mod: Module) -> None:
        if mod.writer:
            mod.writer.close()
            try:
                await mod.writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            mod.writer = None
        if mod.connected:
            mod.connected = False
            async_dispatcher_send(self.hass, signal_connection(self.entry_id))

    async def _run(self, mod: Module) -> None:
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, mod.port), timeout=10
                )
                mod.writer = writer
                mod.connected = True
                mod.last_line = time.monotonic()
                async_dispatcher_send(self.hass, signal_connection(self.entry_id))
                _LOGGER.info("Modul %s verbunden (%s:%s, %s)", mod.module_id, self.host, mod.port, mod.role)
                # Kennung abfragen und Empfangsmodus setzen, während unten schon gelesen wird
                # (die Antworten der Firmware kommen über dieselbe Verbindung)
                init = self.hass.async_create_background_task(
                    self._async_on_connect(mod), f"{DOMAIN}_{self.entry_id}_init_{mod.module_id}"
                )
                try:
                    while True:
                        raw = await reader.readline()
                        if not raw:
                            raise ConnectionError("Verbindung vom Gegenüber geschlossen")
                        line = raw.decode(errors="replace").strip()
                        if line:
                            # Antworten auf eigene Anfragen zählen nicht als Empfang
                            if self._resolve_waiter(mod, line):
                                continue
                            self._note_rx(mod)
                            try:
                                self._handle_line(mod, line)
                            except Exception:  # noqa: BLE001
                                _LOGGER.exception("Fehler beim Verarbeiten von %r", line)
                finally:
                    init.cancel()
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionError, asyncio.TimeoutError) as err:
                level = logging.WARNING if mod.connected else logging.DEBUG
                _LOGGER.log(level, "Modul %s (Port %s): %s", mod.module_id, mod.port, err)
            await self._close(mod)
            await asyncio.sleep(RECONNECT_DELAY)

    async def async_reopen(self, module_id: str) -> None:
        """Verbindung schließen; _run baut sie neu auf und initialisiert das Modul neu."""
        mod = self.modules.get(module_id)
        if mod and mod.writer:
            _LOGGER.info("Modul %s: Verbindung wird neu aufgebaut", module_id)
            mod.writer.close()

    @callback
    def _note_rx(self, mod: Module) -> None:
        """Empfangsstatistik je Modul; Entitäten höchstens einmal pro Minute aktualisieren."""
        now = dt_util.now()
        if now.toordinal() != mod.rx_day:
            mod.rx_day, mod.rx_count = now.toordinal(), 0
        mod.rx_count += 1
        mod.last_line = time.monotonic()
        first = mod.last_rx is None
        mod.last_rx = now
        if first or time.monotonic() - mod.rx_notified > 60:
            mod.rx_notified = time.monotonic()
            async_dispatcher_send(self.hass, signal_rx(self.entry_id))

    # ------------------------------------------------------------------ Firmware-Antworten
    @callback
    def _resolve_waiter(self, mod: Module, line: str) -> bool:
        """Zeile als Antwort auf eine laufende Anfrage verbuchen (dann nicht weiter auswerten)."""
        for item in list(mod.waiters):
            pattern, fut = item
            if not fut.done() and pattern.search(line):
                fut.set_result(line)
                mod.waiters.remove(item)
                return True
        return False

    async def async_request(
        self, module_id: str, command: str, pattern: str, timeout: float = REPLY_TIMEOUT
    ) -> str | None:
        """Befehl senden und auf die erste passende Antwortzeile warten (None = keine Antwort).

        Je Modul läuft höchstens eine Anfrage gleichzeitig, damit Antworten eindeutig bleiben.
        Andere Zeilen (Telegramme) werden währenddessen normal ausgewertet.
        """
        mod = self.modules.get(module_id)
        if mod is None:
            raise ConnectionError(f"Modul {module_id} ist nicht eingerichtet")
        async with mod.req_lock:
            item = (re.compile(pattern), self.hass.loop.create_future())
            mod.waiters.append(item)
            try:
                await self.async_send(module_id, command)
                return await asyncio.wait_for(item[1], timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                if item in mod.waiters:
                    mod.waiters.remove(item)

    async def _async_on_connect(self, mod: Module) -> None:
        if mod.role == ROLE_MONITOR:
            return                     # "Nur überwachen": von sich aus nichts senden
        try:
            await self._async_identify(mod)
            if mod.is_wmbus and self.wmbus_owner() in (None, mod.module_id):
                self._owner_known.set()
            if self.init_mode:
                if mod.role == ROLE_RF433:
                    # Erst wissen, ob und wo wMBus läuft (Firmware, Rolle unterstützt?)
                    try:
                        await asyncio.wait_for(self._owner_known.wait(), STARTUP_SLOWRF_WAIT)
                    except asyncio.TimeoutError:
                        _LOGGER.debug("Modul %s: wMBus-Modul meldet sich nicht", mod.module_id)
                        self._owner_known.set()          # nicht bei jedem Reconnect erneut warten
                elif mod.is_wmbus:
                    # wMBus zuletzt: erst die SlowRF-Module (höchstens STARTUP_SLOWRF_WAIT s)
                    try:
                        await asyncio.wait_for(self._slowrf_ready.wait(), STARTUP_SLOWRF_WAIT)
                    except asyncio.TimeoutError:
                        _LOGGER.debug("Modul %s: SlowRF-Module noch nicht bereit, wMBus trotzdem", mod.module_id)
                await self.async_set_mode(mod.module_id, "Verbinden")
            await self.async_refresh_info(mod.module_id)
        except (OSError, ConnectionError) as err:
            _LOGGER.debug("Modul %s: Initialisierung abgebrochen: %s", mod.module_id, err)

    async def _async_identify(self, mod: Module) -> None:
        """Version ("V") und Kommandoliste ("?") lesen – nur lesend, auch ohne init_mode."""
        ver = await self.async_request(mod.module_id, "V", r"^V ")
        if ver:
            if mod.version and ver != mod.version and mod.unsupported:
                _LOGGER.info("Modul %s: neue Firmware (%s), Rolle wird erneut geprüft", mod.module_id, ver)
                mod.unsupported = None
            mod.version = ver
            if mod.role == ROLE_WMBUS_C and "a-culfw" in ver:
                # "brc" landet in a-culfw im Aus-Zweig (siehe _set_wmbus_locked) – gar nicht senden
                self._mark_unsupported(mod, UNSUPPORTED_NO_CMODE, None)
        reply = await self.async_request(mod.module_id, "?", r"Use one of")
        if reply:
            mod.cmds = parse_cmds(reply)
            _LOGGER.debug("Modul %s: Kommandos %s", mod.module_id, mod.cmds)
            needs = ROLE_NEEDS.get(mod.role, "")
            if mod.is_wmbus and "b" in needs and "b" not in mod.cmds:
                self._mark_unsupported(mod, UNSUPPORTED_NO_B, reply)
        self._persist_mode(mod)
        async_dispatcher_send(self.hass, signal_module_info(self.entry_id))

    @callback
    def _persist_mode(self, mod: Module) -> None:
        self._modes[mod.module_id] = {
            "role": mod.role, "version": mod.version, "cmds": mod.cmds, "unsupported": mod.unsupported,
        }
        self._save_later(5)

    @callback
    def _mark_unsupported(self, mod: Module, reason: str, reply: str | None) -> None:
        if mod.unsupported == reason:
            return
        mod.unsupported = reason
        mod.mode_reply = reply
        text = {
            UNSUPPORTED_NO_B: "die Firmware meldet für dieses Modul kein wMBus-Kommando \"b\"",
            UNSUPPORTED_UNKNOWN: "die Firmware kennt den Befehl nicht",
            UNSUPPORTED_NO_CMODE: "die Firmware hat keinen C-Modus (a-culfw kennt nur brs/brt, "
            "\"brc\" schaltet den Empfang aus)",
        }.get(reason, reason)
        _LOGGER.error(
            "Modul %s: Rolle %s wird nicht unterstützt – %s (%s). Der Befehl wird nicht "
            "gesendet; bitte unter Konfigurieren → Funkmodule eine andere Rolle wählen.",
            mod.module_id, mod.role, text,
            f"Antwort: {reply!r}" if reply is not None else f"Firmware: {mod.version}",
        )
        self._persist_mode(mod)
        self._update_conflicts()
        # Ein bisher blockiertes Modul darf jetzt den wMBus-Empfang übernehmen
        for other in self.modules.values():
            if other.skipped and not other.conflict and other.connected and self.init_mode:
                other.skipped = False
                self.hass.async_create_task(self._async_set_mode_safe(other.module_id, "Modul frei"))

    def wmbus_owner(self) -> str | None:
        """Das eine Modul, das in den wMBus-Modus gesetzt wird.

        a-culfw (clib/rf_mode.c, set_RF_mode) schaltet beim Setzen von S oder T auf einem
        CC1101 jedes andere Modul im wMBus-Modus aus – MBpacket/RXinfo gibt es nur einmal.
        Gleichzeitiger Empfang auf zwei Modulen ist damit nicht möglich: das erste Modul
        (kleinste Nummer) mit unterstützter wMBus-Rolle bekommt den Empfang.
        """
        return next(
            (mid for mid in sorted(self.modules)
             if self.modules[mid].is_wmbus and not self.modules[mid].unsupported),
            None,
        )

    @callback
    def _update_conflicts(self) -> None:
        owner = self.wmbus_owner()
        for mod in self.modules.values():
            mod.conflict = owner if (mod.is_wmbus and not mod.unsupported and owner != mod.module_id) else None

    async def async_set_mode(self, module_id: str, reason: str) -> str | None:
        """Modul in den Betrieb seiner Rolle bringen und die Antwort der Firmware auswerten.

        Idempotent: "X21" und "brs"/"brt" initialisieren den CC1101 jedes Mal neu, egal in
        welchem Zustand er vorher war. Alle Module teilen sich eine Sperre (nacheinander,
        nicht parallel), SlowRF-Module werden vor wMBus initialisiert. Liefert die Antwort
        auf den wMBus-Befehl (bzw. None).
        """
        mod = self.modules.get(module_id)
        if mod is None or not mod.connected:
            return None
        if mod.role == ROLE_RF433:
            await self.async_init_slowrf([module_id], reason)
            return None
        if not mod.is_wmbus:
            return None
        async with self._radio_lock:
            return await self._set_wmbus_locked(mod, reason)

    def _active_owner(self) -> Module | None:
        """Verbundenes Modul, das den wMBus-Empfang hat (falls eingerichtet)."""
        owner = self.wmbus_owner()
        mod = self.modules.get(owner) if owner else None
        return mod if mod is not None and mod.connected else None

    async def async_init_slowrf(self, module_ids: list[str], reason: str) -> None:
        """SlowRF-Module (neu) initialisieren; läuft wMBus, wird er dafür kurz ausgeschaltet."""
        async with self._radio_lock:
            await self._init_slowrf_locked(module_ids, reason, rearm=True)

    async def _init_slowrf_locked(self, module_ids: list[str], reason: str, rearm: bool) -> None:
        """Ablauf (nur mit gehaltener Sperre):

        1. wMBus aus ("brx" an das wMBus-Modul, Antwort "OFF"), falls er laufen könnte –
           sonst lädt "X21" die Konfiguration nicht (a-culfw rf_receive.c, set_txrestore)
        2. "X21" an jedes SlowRF-Modul: set_txreport -> set_RF_mode(slow) -> set_txrestore
           -> ccInitChip(EE_CC1100_CFGn) + ccRX (Empfang und GDO-Interrupt an)
        3. wMBus wieder ein (nur wenn er in Schritt 1 ausgeschaltet wurde und rearm)
        """
        mods = [
            self.modules[m] for m in module_ids
            if m in self.modules and self.modules[m].role == ROLE_RF433 and self.modules[m].connected
        ]
        if not mods:
            return
        owner = self._active_owner()
        paused = False
        # Ohne "Empfangsmodus setzen" wird wMBus nicht angefasst (nur X21 wie bisher)
        if owner is not None and self.init_mode and self._wmbus_on is not False:
            reply = await self.async_request(owner.module_id, WMBUS_OFF_CMD, MODE_REPLY_RE)
            owner.mode_cmd, owner.mode_reply, owner.mode_time = WMBUS_OFF_CMD, reply, dt_util.utcnow()
            if reply == "OFF":
                self._wmbus_on, paused = False, True
            _LOGGER.info(
                "wMBus auf Modul %s kurz ausgeschaltet (%s -> %s), damit Modul %s initialisiert werden kann (%s)",
                owner.module_id, WMBUS_OFF_CMD, reply, ", ".join(m.module_id for m in mods), reason,
            )
        # Ohne eingerichtetes wMBus-Modul schaltet die Integration wMBus nie ein
        known_off = self._wmbus_on is False or self.wmbus_owner() is None
        for mod in mods:
            await self.async_send(mod.module_id, "X21")
            # "X21" hat keine Antwort. Jedes Modul hat eine eigene TCP-Verbindung, die Reihenfolge
            # zwischen Modulen ist also nicht garantiert: erst weiter (wMBus wieder ein), wenn die
            # Firmware ein nachfolgendes "X" (nur lesen) beantwortet hat – dann ist X21 verarbeitet.
            ack = await self.async_request(mod.module_id, "X", r"^[0-9A-F]{2} +\d*$")
            if ack is None:
                _LOGGER.warning("Modul %s: keine Antwort auf X nach X21 (%s)", mod.module_id, reason)
            mod.radio_at, mod.radio_ok = dt_util.utcnow(), known_off
            if known_off:
                _LOGGER.info("Modul %s: SlowRF initialisiert (X21, %s)", mod.module_id, reason)
            else:
                _LOGGER.warning(
                    "Modul %s: X21 gesendet, aber das wMBus-Modul ist nicht verbunden – falls wMBus "
                    "noch läuft, empfängt das Modul erst nach dessen Neuinitialisierung (%s)",
                    mod.module_id, reason,
                )
        self._check_slowrf_ready()
        if paused and rearm and owner is not None and owner.connected and self._wmbus_started:
            await self._set_wmbus_locked(owner, f"{reason}, nach SlowRF")
        async_dispatcher_send(self.hass, signal_module_info(self.entry_id))

    @callback
    def _check_slowrf_ready(self) -> None:
        if all(m.radio_at is not None or not self.init_mode
               for m in self.modules.values() if m.role == ROLE_RF433):
            self._slowrf_ready.set()

    async def _set_wmbus_locked(self, mod: Module, reason: str) -> str | None:
        """wMBus-Modus setzen (nur mit gehaltener Sperre)."""
        module_id = mod.module_id
        if not mod.connected:
            return None
        if mod.unsupported:
            _LOGGER.debug("Modul %s: Rolle %s nicht unterstützt (%s), nichts gesendet",
                          module_id, mod.role, mod.unsupported)
            return None
        if mod.role == ROLE_WMBUS_C and mod.version and "a-culfw" in mod.version:
            # Nicht ausprobieren: "brc" landet in a-culfw in set_RF_mode(RF_mode_off), und der
            # setzt die globale Variable mbus_mode = WMBUS_NONE (clib/rf_mode.c) – das beendet
            # auch den Empfang eines anderen Moduls im S- oder T-Modus.
            self._mark_unsupported(mod, UNSUPPORTED_NO_CMODE, None)
            async_dispatcher_send(self.hass, signal_module_info(self.entry_id))
            return None
        self._update_conflicts()
        if mod.conflict:
            mod.skipped = True
            _LOGGER.warning(
                "Modul %s: Empfangsmodus %s nicht gesetzt – die Firmware erlaubt nur ein Modul im "
                "wMBus-Modus, Modul %s ist dafür eingerichtet", module_id, mod.role, mod.conflict,
            )
            async_dispatcher_send(self.hass, signal_module_info(self.entry_id))
            return None
        mod.skipped = False
        # SlowRF-Module, die noch nicht sicher initialisiert sind, zuerst (wMBus ist danach aus)
        pending = [m.module_id for m in self.modules.values()
                   if m.role == ROLE_RF433 and m.connected and not m.radio_ok]
        if pending:
            await self._init_slowrf_locked(pending, reason, rearm=False)
        cmd = ROLE_MODE_CMD[mod.role]
        await self.async_send(module_id, "X21")           # RSSI/LQI anhängen (schaltet auf SlowRF)
        reply = await self.async_request(module_id, cmd, MODE_REPLY_RE)
        self._wmbus_started = True
        mod.mode_cmd, mod.mode_time = cmd, dt_util.utcnow()
        expected = MODE_REPLY[cmd]
        if reply is None:
            mod.mode_reply = None
            self._wmbus_on = None
            _LOGGER.warning("Modul %s: keine Antwort auf %s (%s)", module_id, cmd, reason)
        elif "is unknown" in reply:
            self._mark_unsupported(mod, UNSUPPORTED_UNKNOWN, reply)
        elif reply == expected:
            mod.mode_reply = reply
            self._wmbus_on = True
            _LOGGER.info("Modul %s: Empfangsmodus gesetzt (%s -> %s, %s)", module_id, cmd, reply, reason)
        elif reply == "OFF" and mod.role == ROLE_WMBUS_C:
            self._wmbus_on = False
            self._mark_unsupported(mod, UNSUPPORTED_NO_CMODE, reply)
        else:
            mod.mode_reply = reply
            self._wmbus_on = reply in ("SMODE", "TMODE")
            _LOGGER.warning("Modul %s: %s beantwortet mit %r statt %s (%s)", module_id, cmd, reply, expected, reason)
        async_dispatcher_send(self.hass, signal_module_info(self.entry_id))
        return reply

    @callback
    def _unsolicited_mode(self, mod: Module, line: str) -> None:
        """Statuszeile, die nicht auf eine eigene Anfrage folgt (z. B. von einem anderen Client)."""
        mod.mode_reply, mod.mode_time = line, dt_util.utcnow()
        if line in ("SMODE", "TMODE"):
            self._wmbus_on = True                 # Status ist global (mbus_mode)
        elif line == "OFF":
            self._wmbus_on = False
        async_dispatcher_send(self.hass, signal_module_info(self.entry_id))
        if line != "OFF" or not self.init_mode or mod.unsupported or mod.conflict:
            return
        now = time.monotonic()
        if now - mod.mode_retry < MODE_RETRY_DELAY:
            _LOGGER.debug("Modul %s meldet erneut OFF – nächster Versuch frühestens in %d s",
                          mod.module_id, MODE_RETRY_DELAY - (now - mod.mode_retry))
            return
        mod.mode_retry = now
        _LOGGER.warning("Modul %s meldet OFF, setze Empfangsmodus erneut", mod.module_id)
        self.hass.async_create_task(self._async_set_mode_safe(mod.module_id, "OFF gemeldet"))

    async def _async_set_mode_safe(self, module_id: str, reason: str) -> None:
        try:
            await self.async_set_mode(module_id, reason)
        except (OSError, ConnectionError) as err:
            _LOGGER.debug("Modul %s: Empfangsmodus nicht gesetzt: %s", module_id, err)

    async def _async_watchdog(self, _now=None) -> None:
        """Modul ohne jede Zeile: einmal neu initialisieren, höchstens einmal pro Stunde.

        wMBus nach 45 min (Zähler senden selten), SlowRF/433 MHz nach 15 min. Läuft über
        dieselbe Sperre und Reihenfolge wie beim Verbinden (SlowRF: wMBus kurz aus).
        """
        if not self.init_mode:
            return
        now = time.monotonic()
        for mod in list(self.modules.values()):
            if not mod.connected:
                continue
            if mod.role == ROLE_RF433:
                limit = WATCHDOG_SILENCE_SLOWRF
            elif mod.is_wmbus and not mod.unsupported and not mod.conflict:
                limit = WATCHDOG_SILENCE
            else:
                continue
            silent = now - mod.last_line
            if silent < limit or now - mod.watchdog_at < WATCHDOG_REPEAT:
                continue
            mod.watchdog_at = now
            _LOGGER.warning(
                "Modul %s: seit %d min keine Zeile empfangen – %s wird neu initialisiert",
                mod.module_id, silent // 60, "SlowRF" if mod.role == ROLE_RF433 else "Empfangsmodus",
            )
            await self._async_set_mode_safe(mod.module_id, "Wächter")

    async def async_send_it(self, module_id: str, command: str) -> None:
        """Intertechno senden ("is…").

        a-culfw stellt danach den SlowRF-Empfang über set_txrestore wieder her
        (intertechno.c, restore_RF_mode) – das passiert bei aktivem wMBus nicht: der Chip
        bleibt in der Intertechno-Konfiguration, der GDO-Interrupt aus. Deshalb anschließend
        wie beim Verbinden neu initialisieren (wMBus kurz aus).
        """
        await self.async_send(module_id, command)
        if not self.init_mode or self._active_owner() is None or self._wmbus_on is False:
            return
        if (old := self._it_reinit.get(module_id)) is not None and not old.done():
            old.cancel()                       # mehrere Sendebefehle kurz hintereinander: einmal danach
        self._it_reinit[module_id] = self.hass.async_create_task(self._async_reinit_after_it(module_id))

    async def _async_reinit_after_it(self, module_id: str) -> None:
        await asyncio.sleep(IT_REINIT_DELAY)
        await self._async_set_mode_safe(module_id, "nach Intertechno-Senden")

    async def async_send(self, module_id: str, command: str) -> None:
        mod = self.modules.get(module_id)
        if not mod or not mod.writer or not mod.connected:
            raise ConnectionError(f"Modul {module_id} ist nicht verbunden")
        async with mod.lock:
            _LOGGER.debug("Modul %s -> %s", module_id, command)
            mod.writer.write(command.encode() + b"\r\n")
            await mod.writer.drain()

    # ------------------------------------------------------------------ Modulwerte (wie FHEM "get")
    async def async_refresh_info(self, module_id: str, force: bool = False) -> dict:
        """Version, Kommandos, ccconf und – höchstens alle 15 min – credit10ms und Laufzeit lesen.

        Nur lesende Befehle (V, ?, Cxx, X, t). Kein Dauer-Polling: Aufruf beim Verbinden und
        über den Button "Modulwerte aktualisieren".
        """
        mod = self.modules.get(module_id)
        if mod is None or not mod.connected:
            return {}
        now = time.monotonic()
        info = mod.info
        if force or mod.version is None or mod.cmds is None:
            await self._async_identify(mod)
        info["version"], info["cmds"] = mod.version, mod.cmds
        cmds = mod.cmds or ""
        if "C" in cmds or not cmds:
            regs = await self._async_read_regs(module_id)
            if regs:
                info["regs"] = {f"{r:02X}": v for r, v in regs.items()}
                info.update(culcmd.ccconf(regs))
        due = now - mod.info_at.get("slow", -1e9) >= INFO_SLOW_INTERVAL
        if due:
            mod.info_at["slow"] = now
            if "X" in cmds or not cmds:
                reply = await self.async_request(module_id, "X", r"^[0-9A-F]{2} +\d*$")
                if reply and (credit := culcmd.parse_credit(reply)) is not None:
                    info["credit10ms"] = credit
            if "t" in cmds:
                reply = await self.async_request(module_id, "t", r"^[0-9A-F]{8}$")
                if reply and (secs := culcmd.parse_uptime(reply)) is not None:
                    info["uptime"] = secs
        info["updated"] = dt_util.utcnow()
        async_dispatcher_send(self.hass, signal_module_info(self.entry_id))
        return info

    async def async_refresh_all(self) -> None:
        for mid in sorted(self.modules):
            try:
                await self.async_refresh_info(mid, force=True)
            except (OSError, ConnectionError) as err:
                _LOGGER.debug("Modul %s: Modulwerte nicht gelesen: %s", mid, err)

    async def _async_read_regs(self, module_id: str) -> dict[int, int]:
        regs: dict[int, int] = {}
        for reg in culcmd.CCCONF_REGS:
            reply = await self.async_request(module_id, f"C{reg:02X}", rf"^C{reg:02X} = ")
            parsed = culcmd.parse_reg(reply) if reply else None
            if parsed is None:
                return {}
            regs[parsed[0]] = parsed[1]
        return regs

    # ------------------------------------------------------------------ Einstellungen (wie FHEM "set")
    def can_configure_radio(self, module_id: str) -> bool:
        """freq/bWidth/rAmpl/sens/ITClock nur im SlowRF-Betrieb (FHEM: rfmode SlowRF).

        In einer wMBus-Rolle lädt die Firmware eigene Funkeinstellungen (rf_mbus_init).
        """
        mod = self.modules.get(module_id)
        return bool(mod and mod.role == ROLE_RF433 and mod.cmds and "W" in mod.cmds)

    async def _async_backup_ccconf(self, module_id: str) -> None:
        """Vor dem ersten Schreiben die aktuelle Konfiguration lesen und ins Log schreiben."""
        mod = self.modules[module_id]
        if mod.info.get("backup"):
            return
        regs = await self._async_read_regs(module_id)
        if not regs:
            raise ConnectionError(f"Modul {module_id}: ccconf nicht lesbar – nichts geschrieben")
        conf = culcmd.ccconf(regs)
        mod.info["backup"] = {"regs": {f"{r:02X}": v for r, v in regs.items()}, **conf}
        _LOGGER.warning(
            "Modul %s: Sicherung vor dem Schreiben ins EEPROM – freq %.3f MHz, bWidth %d kHz, "
            "rAmpl %d dB, sens %d dB, Register %s", module_id, conf["freq"], conf["bwidth"],
            conf["rampl"], conf["sens"], " ".join(f"C{r:02X}={v:02X}" for r, v in regs.items()),
        )

    async def async_write_radio(self, module_id: str, setting: str, value) -> object:
        """freq/bwidth/rampl/sens ins EEPROM schreiben (nur nach Nutzeraktion aufrufen)."""
        if not self.can_configure_radio(module_id):
            raise ValueError(f"Modul {module_id}: Funkeinstellungen nur im 433-MHz-Betrieb änderbar")
        await self._async_backup_ccconf(module_id)
        mod = self.modules[module_id]
        if setting == "freq":
            cmds, actual = culcmd.freq_cmds(float(value))
        elif setting == "bwidth":
            regs = await self._async_read_regs(module_id)
            if not regs:
                raise ConnectionError(f"Modul {module_id}: MDMCFG4 nicht lesbar")
            cmd, actual = culcmd.bwidth_cmd(float(value), regs[0x10])
            cmds = [cmd]
        elif setting == "rampl":
            cmd, actual = culcmd.rampl_cmd(int(value))
            cmds = [cmd]
        elif setting == "sens":
            cmd, actual = culcmd.sens_cmd(int(value))
            cmds = [cmd]
        else:
            raise ValueError(setting)
        for cmd in cmds:
            await self.async_send(module_id, cmd)
        _LOGGER.warning("Modul %s: %s = %s ins EEPROM geschrieben (%s)", module_id, setting, actual, " ".join(cmds))
        await self._async_reload_radio(mod)
        return actual

    async def _async_reload_radio(self, mod: Module) -> None:
        """Neue EEPROM-Werte laden: X21 (set_txreport -> set_txrestore -> ccInitChip).

        a-culfw überspringt das, solange irgendein Modul im wMBus-Modus ist – deshalb wie
        beim Verbinden: wMBus kurz aus, X21, wMBus wieder ein.
        """
        await self.async_init_slowrf([mod.module_id], "neue Funkeinstellungen")
        await self.async_refresh_info(mod.module_id)

    async def async_set_patable(self, module_id: str, idx: str) -> None:
        """PA-Tabelle (Sendeleistung). a-culfw schreibt immer in den EEPROM-Bereich von Modul 1
        und lädt dessen Konfiguration in den Chip – deshalb nur für Modul 1 im 433-MHz-Betrieb."""
        if module_id != "1" or not self.can_configure_radio(module_id) or "x" not in (self.modules[module_id].cmds or ""):
            raise ValueError("patable nur für Modul 1 im 433-MHz-Betrieb")
        await self._async_backup_ccconf(module_id)
        await self.async_send(module_id, culcmd.patable_cmd(idx))
        _LOGGER.warning("Modul %s: patable %s ins EEPROM geschrieben", module_id, idx)

    async def async_set_led(self, module_id: str, mode: str) -> None:
        await self.async_send(module_id, culcmd.led_cmd(mode))
        _LOGGER.info("Modul %s: LED %s (wird im EEPROM gespeichert)", module_id, mode)

    async def async_set_itclock(self, module_id: str, us: int) -> None:
        if not self.can_configure_radio(module_id) or "i" not in (self.modules[module_id].cmds or ""):
            raise ValueError("ITClock nur am 433-MHz-Modul")
        await self.async_send(module_id, culcmd.itclock_cmd(us))
        _LOGGER.info("Modul %s: ITClock %d µs (nur bis zum Neustart des MapleCUN)", module_id, us)

    async def async_send_raw(self, module_id: str, command: str, force: bool = False,
                             timeout: float = REPLY_TIMEOUT) -> str | None:
        """Beliebiges Kommando (FHEM "set raw"/"get raw"); erste Antwortzeile oder None.

        "B…" (Bootloader/Neustart) und "e…" (Werkseinstellungen) nur mit force.
        Bei wMBus-Modulen zählen Telegrammzeilen ("b…") nicht als Antwort.
        """
        if not command:
            raise ValueError("leeres Kommando")
        if culcmd.raw_blocked(command) and not force:
            raise PermissionError(f"Kommando {command!r} nur mit force: true")
        mod = self.modules.get(module_id)
        if mod is None:
            raise ConnectionError(f"Modul {module_id} ist nicht eingerichtet")
        pattern = r"^(?!b[0-9A-FY])" if mod.is_wmbus else r"."
        _LOGGER.info("Modul %s: send_raw %r%s", module_id, command, " (force)" if force else "")
        reply = await self.async_request(module_id, command, pattern, timeout)
        async_dispatcher_send(
            self.hass, signal_raw(self.entry_id, module_id), f"{command} -> {reply if reply is not None else '(keine Antwort)'}"
        )
        return reply

    # ------------------------------------------------------------------ Geräteerkennung
    def is_configured(self, key: str) -> bool:
        kind, _, ident = key.partition(":")
        return (
            (kind == KIND_METER and ident in self.meters)
            or (kind == KIND_REVOLT and ident in self.revolts)
            or (kind == KIND_IT and ident in self.it_codes)
            or (kind == KIND_SENSOR and ident in self.climate)
        )

    @callback
    def _discover(self, kind: str, ident: str, info: dict) -> None:
        """Neues bzw. erneut gehörtes, noch nicht eingerichtetes Gerät verbuchen."""
        key = f"{kind}:{ident}"
        if key in self.ignored or self.is_configured(key):
            return
        info = {**info, "last_seen": dt_util.now().isoformat(timespec="seconds")}
        is_new = key not in self.discovered
        self.discovered[key] = info
        self._save_later(30)
        if not is_new:
            return
        _LOGGER.info("Neues Gerät gefunden: %s %s", kind, ident)
        if self.auto.get(kind) and key not in self._adopting:
            self._adopting.add(key)
            self.hass.async_create_task(self.async_adopt([key]))
            return
        async_dispatcher_send(self.hass, signal_discovered(self.entry_id))
        self._update_notification()

    @callback
    def _update_notification(self) -> None:
        nid = f"{DOMAIN}_{self.entry_id}_discovered"
        count = len(self.discovered)
        if count == 0:
            persistent_notification.async_dismiss(self.hass, nid)
            self._notified = 0
            return
        if count <= self._notified:
            return
        self._notified = count
        persistent_notification.async_create(
            self.hass,
            f"Der MapleCUN hat **{count} neue Geräte** gefunden.\n\n"
            "Übernehmen oder ignorieren unter **Einstellungen → Geräte & Dienste → MapleCUN → "
            "Konfigurieren → Gefundene Geräte**.",
            title="MapleCUN: neue Geräte",
            notification_id=nid,
        )

    def discovered_label(self, key: str) -> str:
        kind, _, ident = key.partition(":")
        info = self.discovered.get(key, {})
        rssi = f", {info['rssi']} dBm" if info.get("rssi") is not None else ""
        if kind == KIND_METER:
            extra = ", verschlüsselt" if info.get("encrypted") else ""
            via = ", über Netzwerkknoten" if info.get("relayed") else ""
            return f"Zähler {ident} – {info.get('manufacturer', '?')} {info.get('type', '')}{rssi}{extra}{via}"
        if kind == KIND_IT:
            return f"Intertechno {ident} (zuletzt Befehl {info.get('cmd', '?')})"
        if kind == KIND_REVOLT:
            return f"Revolt {ident} – {info.get('power', '?')} W"
        if kind == KIND_SENSOR:
            hum = f", {info['humidity']} %" if info.get("humidity") is not None else ""
            return f"Thermometer {info.get('model', '?')} {ident.split('_', 1)[-1]} – {info.get('temperature')} °C{hum}{rssi}"
        return key

    def adopt_options(self, keys: list[str]) -> dict:
        """Neue Optionen mit übernommenen Geräten bauen (entfernt sie aus 'gefunden')."""
        opts = dict(self.entry.options)
        meters = dict(opts.get(CONF_METERS, {}))
        revolts = dict(opts.get(CONF_REVOLTS, {}))
        its = dict(opts.get(CONF_IT_DEVICES, {}))
        sensors = dict(opts.get(CONF_SENSORS, {}))
        for key in keys:
            kind, _, ident = key.partition(":")
            info = self.discovered.pop(key, {})
            if kind == KIND_METER:
                meters.setdefault(ident, {CONF_NAME: f"{info.get('type', 'Zähler')} {ident}", CONF_KEY: ""})
            elif kind == KIND_REVOLT:
                revolts.setdefault(ident, {CONF_NAME: f"Revolt {ident}"})
            elif kind == KIND_IT:
                its.setdefault(
                    ident, {CONF_NAME: f"Intertechno {ident}", CONF_ON: DEFAULT_IT_ON, CONF_OFF: DEFAULT_IT_OFF}
                )
            elif kind == KIND_SENSOR:
                sensors.setdefault(ident, {CONF_NAME: f"Thermometer {ident}", CONF_MODEL: info.get("model", "")})
        self._save_later(1)
        self._update_notification()
        return {**opts, CONF_METERS: meters, CONF_REVOLTS: revolts, CONF_IT_DEVICES: its, CONF_SENSORS: sensors}

    async def async_adopt(self, keys: list[str]) -> None:
        """Automatisch übernehmen (Integration lädt danach neu)."""
        new_opts = self.adopt_options(keys)
        await self._store.async_save(self._store_data())
        self.hass.config_entries.async_update_entry(self.entry, options=new_opts)

    def discovered_sorted(self) -> list[str]:
        """Steckdosen, Revolts, dann Zähler nach Signalstärke (nahe zuerst)."""
        order = {KIND_IT: 0, KIND_SENSOR: 1, KIND_REVOLT: 2, KIND_METER: 3}

        def sort_key(k: str):
            info = self.discovered[k]
            rssi = info.get("rssi")
            return (order.get(k.partition(":")[0], 9), -(rssi if rssi is not None else -999), k)

        return sorted(self.discovered, key=sort_key)

    async def async_ignore(self, keys: list[str]) -> None:
        for key in keys:
            self.discovered.pop(key, None)
            self.ignored.add(key)
        await self._store.async_save(self._store_data())
        self._update_notification()
        async_dispatcher_send(self.hass, signal_discovered(self.entry_id))

    async def async_reset_ignored(self) -> None:
        self.ignored.clear()
        await self._store.async_save(self._store_data())

    # ------------------------------------------------------------------ Empfang
    @callback
    def _handle_line(self, mod: Module, line: str) -> None:
        if mod.role in WMBUS_ROLES and line.startswith("b"):
            # Rohdaten-Sensor (standardmäßig aus) zeigt bei wMBus jede Telegrammzeile
            async_dispatcher_send(self.hass, signal_raw(self.entry_id, mod.module_id), line)
            self._handle_wmbus(mod, line)
            return
        if mod.role == ROLE_RF433:
            if revolt.is_revolt(line):
                self._handle_revolt(line)
                return
            if (it := intertechno.decode(line)) is not None:
                self._handle_it(*it)
                return
            if (reading := climate433.decode(line)) is not None:
                self._handle_climate(reading)
                return
        if line in ("OFF", "CMODE", "TMODE", "SMODE"):
            if mod.role in WMBUS_ROLES:
                self._unsolicited_mode(mod, line)
            return
        if line.startswith("V "):
            return
        async_dispatcher_send(self.hass, signal_raw(self.entry_id, mod.module_id), line)

    # --- Wireless M-Bus
    @callback
    def _handle_wmbus(self, mod: Module, line: str) -> None:
        try:
            frame, lqi, rssi = wmbus.parse_cul_line_status(line)
            # Zuerst nur die Kopfdaten: von fremden Zählern wird nichts entschlüsselt
            # oder dekodiert
            head = wmbus.decode(frame, header_only=True)
        except wmbus.WMBusError as err:
            _LOGGER.debug("Modul %s: Telegramm verworfen (%s): %s", mod.module_id, err, line)
            return
        mid = head.meter_id
        self._note_heard(mod, head, rssi)
        if mid not in self.meters:
            # Fremder Zähler (z. B. Nachbarn): Messwerte verwerfen, nichts speichern
            if self.log_unknown:
                _LOGGER.info(
                    "Unbekannter Zähler %s, Typ 0x%02X (%s), RSSI %s dBm",
                    mid, head.device_type, head.type_name, rssi if rssi is not None else "?",
                )
            return
        try:
            tele = wmbus.decode(frame, {k: v for k, v in self.meters.items() if v})
        except wmbus.WMBusError as err:
            _LOGGER.warning("Zähler %s: %s", err.meter_id or mid, err)
            return
        tele.rssi = rssi
        if tele.meter_id in self.meters:
            self.deliver(tele, rssi, dt_util.utcnow(), mod.module_id, line=line, lqi=lqi)

    @callback
    def deliver(
        self, tele: wmbus.Telegram, rssi: float | None, now, module_id: str | None,
        line: str | None = None, lqi: int | None = None,
    ) -> None:
        """Telegramm eines angelegten Zählers übernehmen.

        Gemeinsamer Codepfad für Live-Empfang und FHEM-Import: Kopfdaten speichern,
        Rohzeile melden, Entitäten anlegen bzw. aktualisieren. now = Empfangszeitpunkt (UTC).
        Nur für angelegte Zähler aufrufen – von fremden Zählern wird nichts gespeichert.
        """
        mid = tele.meter_id
        if mid not in self.meters:
            return
        if module_id is None:
            module_id = self.seen.get(mid, {}).get("module")
        self.seen[mid] = {
            "manufacturer": tele.manufacturer,
            "type": tele.type_name,
            "device_type": tele.device_type,
            "version": tele.version,
            "encrypted": tele.encrypted,
            "rssi": rssi,
            "relayed": tele.relayed,
            "module": module_id,
            "last_seen": dt_util.as_local(now).isoformat(timespec="seconds"),
        }
        if len(self.seen) > SEEN_MAX:
            oldest = min(self.seen, key=lambda k: self.seen[k]["last_seen"])
            self.seen.pop(oldest, None)
        self._save_later(60)

        first = mid not in self.meter_raw
        if line is not None:
            self.meter_raw[mid] = {
                "line": line,
                "module": module_id,
                "relayed": tele.relayed,
                "received": now,
                "meter_time": tele.values().get("datetime") if tele.records else None,
                "lqi": lqi,
                "rssi": rssi,
                "inner": tele.inner.hex().upper() if tele.inner else None,
            }

        if tele.encrypted and not tele.records:
            _LOGGER.warning("Zähler %s sendet verschlüsselt – bitte AES-Schlüssel eintragen", mid)
            if first and line is not None:
                async_dispatcher_send(self.hass, signal_meter_new(self.entry_id), mid, tele, [], {})
            self._send_raw(mid)
            return

        values = tele.values()
        if rssi is not None:
            values["rssi"] = rssi
        values["last_received"] = now
        new_keys = [k for k in values if k not in self.meter_keys.get(mid, [])]
        if new_keys:
            self.meter_keys.setdefault(mid, []).extend(new_keys)
            self._save_later(1)
        if new_keys or (first and line is not None):
            async_dispatcher_send(self.hass, signal_meter_new(self.entry_id), mid, tele, new_keys, values)
        async_dispatcher_send(self.hass, signal_meter(self.entry_id, mid), values)
        self._send_raw(mid)

    @callback
    def _send_raw(self, mid: str) -> None:
        if (info := self.meter_raw.get(mid)) is not None:
            async_dispatcher_send(self.hass, signal_meter_raw(self.entry_id, mid), info)

    @callback
    def _note_heard(self, mod: Module, head: wmbus.Telegram, rssi: float | None) -> None:
        """Kopfdaten eines empfangenen Zählers für die Übersicht merken (nur RAM)."""
        now = dt_util.now()
        day = now.toordinal()
        info = self.heard.get(head.meter_id)
        if info is None:
            if len(self.heard) >= HEARD_MAX:
                oldest = min(
                    (k for k in self.heard if k not in self.meters),
                    key=lambda k: self.heard[k]["last_seen"], default=None,
                )
                if oldest is not None:
                    self.heard.pop(oldest)
            info = self.heard[head.meter_id] = {"count_today": 0, "day": day}
        if info["day"] != day:
            info["day"], info["count_today"] = day, 0
        info.update(
            manufacturer=head.manufacturer,
            type=head.type_name,
            device_type=head.device_type,
            rssi=rssi,
            module=mod.module_id,
            relayed=head.relayed,
            encrypted=head.encrypted,
            last_seen=now.isoformat(timespec="seconds"),
        )
        info["count_today"] += 1
        async_dispatcher_send(self.hass, signal_seen(self.entry_id))

    def heard_today(self, info: dict) -> int:
        return info.get("count_today", 0) if info.get("day") == dt_util.now().toordinal() else 0

    def heard_unconfigured(self) -> list[tuple[str, dict]]:
        """Gehörte, nicht angelegte Zähler, stärkstes Signal zuerst."""
        rows = [(mid, info) for mid, info in self.heard.items() if mid not in self.meters]
        rows.sort(key=lambda kv: (kv[1].get("rssi") is None, -(kv[1].get("rssi") or 0), kv[0]))
        return rows

    # --- Revolt
    @callback
    def _handle_revolt(self, line: str) -> None:
        rid, values = revolt.decode(line)
        if not revolt.plausible(values):
            _LOGGER.debug("Revolt %s unplausibel, verworfen: %s", rid, line)
            return
        if rid not in self.revolts:
            self._discover(KIND_REVOLT, rid, {"power": values["power"]})
            return
        now = time.monotonic()
        last = self._last_energy.get(rid)
        if last:
            e0, t0 = last
            if values["energy"] - e0 > revolt.MAX_KW * (now - t0) / 3600 + 0.02:
                values["energy"] = e0
            else:
                self._last_energy[rid] = (values["energy"], now)
        else:
            self._last_energy[rid] = (values["energy"], now)
        async_dispatcher_send(self.hass, signal_revolt(self.entry_id, rid), values)

    # --- Intertechno (Fernbedienung / Wandschalter gedrückt)
    @callback
    def _handle_it(self, code: str, cmd: str) -> None:
        if code in self.it_codes:
            async_dispatcher_send(self.hass, signal_it(self.entry_id, code), cmd)
        else:
            self._discover(KIND_IT, code, {"cmd": cmd})

    # --- Temperatur-/Feuchtesensoren
    @callback
    def _handle_climate(self, r: climate433.ClimateReading) -> None:
        values = {"temperature": r.temperature, "humidity": r.humidity,
                  "battery_low": r.battery_low, "rssi": r.rssi}
        if r.key not in self.climate:
            self._discover(KIND_SENSOR, r.key, {"model": r.model, **values})
            return
        async_dispatcher_send(self.hass, signal_climate(self.entry_id, r.key), values)

    # ------------------------------------------------------------------ Aufräumen
    async def async_forget_meter(self, meter_id: str) -> None:
        self.meter_keys.pop(meter_id, None)
        self.meter_raw.pop(meter_id, None)
        await self._store.async_save(self._store_data())
