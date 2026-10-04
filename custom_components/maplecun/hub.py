"""Verbindungen zu den Funkmodulen des MapleCUN, Geräteerkennung und Datenverteilung."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import intertechno, revolt, wmbus
from .const import (
    CONF_AUTO,
    CONF_INIT,
    CONF_IT_DEVICES,
    CONF_KEY,
    CONF_METERS,
    CONF_MODULES,
    CONF_NAME,
    CONF_OFF,
    CONF_ON,
    CONF_REVOLTS,
    CONF_ROLE,
    DEFAULT_AUTO,
    DEFAULT_IT_OFF,
    DEFAULT_IT_ON,
    DOMAIN,
    KIND_IT,
    KIND_METER,
    KIND_REVOLT,
    RECONNECT_DELAY,
    ROLE_INIT,
    ROLE_OFF,
    ROLE_RF433,
    SEEN_MAX,
    STORAGE_VERSION,
    WMBUS_ROLES,
    signal_connection,
    signal_discovered,
    signal_it,
    signal_meter,
    signal_meter_new,
    signal_raw,
    signal_revolt,
    signal_seen,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class Module:
    """Ein CC1101-Funkmodul (= ein Port am Split-Proxy)."""

    module_id: str
    port: int
    role: str
    connected: bool = False
    writer: asyncio.StreamWriter | None = None
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MapleCunHub:
    """Verwaltet alle Modul-Verbindungen eines MapleCUN."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id: str = entry.entry_id
        self.host: str = entry.data["host"]
        opts = entry.options
        self.init_mode: bool = opts.get(CONF_INIT, True)
        self.auto: dict[str, bool] = {**DEFAULT_AUTO, **opts.get(CONF_AUTO, {})}
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

        # Laufzeitdaten (gespeichert)
        self.meter_keys: dict[str, list[str]] = {}
        self.seen: dict[str, dict] = {}          # alle gehörten Wireless-M-Bus-Zähler
        self.discovered: dict[str, dict] = {}    # "kind:id" -> Info, noch nicht übernommen
        self.ignored: set[str] = set()
        self.legacy_revolts: list[str] = []      # aus Version 0.1/0.2 (Speicher)

        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{self.entry_id}")
        self._last_energy: dict[str, tuple[float, float]] = {}
        self._adopting: set[str] = set()
        self._notified = 0

    # ------------------------------------------------------------------ Lebenszyklus
    async def async_start(self) -> None:
        data = await self._store.async_load() or {}
        self.legacy_revolts = list(data.get("revolts", []))
        self.meter_keys = {k: list(v) for k, v in data.get("meter_keys", {}).items() if k in self.meters}
        self.seen = data.get("seen", {})
        self.ignored = set(data.get("ignored", []))
        self.discovered = {
            k: v for k, v in data.get("discovered", {}).items()
            if k not in self.ignored and not self.is_configured(k)
        }
        for mod in self.modules.values():
            mod.task = self.hass.async_create_background_task(
                self._run(mod), f"{DOMAIN}_{self.entry_id}_module_{mod.module_id}"
            )
        self._update_notification()

    async def async_stop(self) -> None:
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
        }

    def _save_later(self, delay: float = 10) -> None:
        self._store.async_delay_save(self._store_data, delay)

    # ------------------------------------------------------------------ Verbindung
    def module_for_role(self, role: str) -> Module | None:
        return next((m for m in self.modules.values() if m.role == role), None)

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
                async_dispatcher_send(self.hass, signal_connection(self.entry_id))
                _LOGGER.info("Modul %s verbunden (%s:%s, %s)", mod.module_id, self.host, mod.port, mod.role)
                if self.init_mode:
                    for cmd in ROLE_INIT.get(mod.role, ()):
                        await self.async_send(mod.module_id, cmd)
                while True:
                    raw = await reader.readline()
                    if not raw:
                        raise ConnectionError("Verbindung vom Gegenüber geschlossen")
                    line = raw.decode(errors="replace").strip()
                    if line:
                        try:
                            self._handle_line(mod, line)
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception("Fehler beim Verarbeiten von %r", line)
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionError, asyncio.TimeoutError) as err:
                level = logging.WARNING if mod.connected else logging.DEBUG
                _LOGGER.log(level, "Modul %s (Port %s): %s", mod.module_id, mod.port, err)
            await self._close(mod)
            await asyncio.sleep(RECONNECT_DELAY)

    async def async_send(self, module_id: str, command: str) -> None:
        mod = self.modules.get(module_id)
        if not mod or not mod.writer or not mod.connected:
            raise ConnectionError(f"Modul {module_id} ist nicht verbunden")
        async with mod.lock:
            _LOGGER.debug("Modul %s -> %s", module_id, command)
            mod.writer.write(command.encode() + b"\r\n")
            await mod.writer.drain()

    # ------------------------------------------------------------------ Geräteerkennung
    def is_configured(self, key: str) -> bool:
        kind, _, ident = key.partition(":")
        return (
            (kind == KIND_METER and ident in self.meters)
            or (kind == KIND_REVOLT and ident in self.revolts)
            or (kind == KIND_IT and ident in self.it_codes)
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
            via = f", über Repeater {info['via']}" if info.get("via") else ""
            return f"Zähler {ident} – {info.get('manufacturer', '?')} {info.get('type', '')}{rssi}{extra}{via}"
        if kind == KIND_IT:
            return f"Intertechno {ident} (zuletzt Befehl {info.get('cmd', '?')})"
        if kind == KIND_REVOLT:
            return f"Revolt {ident} – {info.get('power', '?')} W"
        return key

    def adopt_options(self, keys: list[str]) -> dict:
        """Neue Optionen mit übernommenen Geräten bauen (entfernt sie aus 'gefunden')."""
        opts = dict(self.entry.options)
        meters = dict(opts.get(CONF_METERS, {}))
        revolts = dict(opts.get(CONF_REVOLTS, {}))
        its = dict(opts.get(CONF_IT_DEVICES, {}))
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
        self._save_later(1)
        self._update_notification()
        return {**opts, CONF_METERS: meters, CONF_REVOLTS: revolts, CONF_IT_DEVICES: its}

    async def async_adopt(self, keys: list[str]) -> None:
        """Automatisch übernehmen (Integration lädt danach neu)."""
        new_opts = self.adopt_options(keys)
        await self._store.async_save(self._store_data())
        self.hass.config_entries.async_update_entry(self.entry, options=new_opts)

    def discovered_sorted(self) -> list[str]:
        """Steckdosen, Revolts, dann Zähler nach Signalstärke (nahe zuerst)."""
        order = {KIND_IT: 0, KIND_REVOLT: 1, KIND_METER: 2}

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
            self._handle_wmbus(mod, line)
            return
        if mod.role == ROLE_RF433:
            if revolt.is_revolt(line):
                self._handle_revolt(line)
                return
            if (it := intertechno.decode(line)) is not None:
                self._handle_it(*it)
                return
        if line in ("OFF", "CMODE", "TMODE", "SMODE") or line.startswith("V "):
            return
        async_dispatcher_send(self.hass, signal_raw(self.entry_id, mod.module_id), line)

    # --- Wireless M-Bus
    @callback
    def _handle_wmbus(self, mod: Module, line: str) -> None:
        try:
            frame, rssi = wmbus.parse_cul_line(line)
            tele = wmbus.decode(frame, {k: v for k, v in self.meters.items() if v})
        except wmbus.WMBusError as err:
            _LOGGER.debug("Modul %s: Telegramm verworfen (%s): %s", mod.module_id, err, line)
            return
        tele.rssi = rssi
        mid = tele.meter_id
        info = {
            "manufacturer": tele.manufacturer,
            "type": tele.type_name,
            "version": tele.version,
            "encrypted": tele.encrypted,
            "rssi": rssi,
            "via": tele.via,
            "module": mod.module_id,
        }
        self.seen[mid] = {**info, "last_seen": dt_util.now().isoformat(timespec="seconds")}
        if len(self.seen) > SEEN_MAX:
            oldest = min(self.seen, key=lambda k: self.seen[k]["last_seen"])
            self.seen.pop(oldest, None)
        self._save_later(60)
        async_dispatcher_send(self.hass, signal_seen(self.entry_id))

        if mid not in self.meters:
            self._discover(KIND_METER, mid, info)
            return
        if tele.encrypted and not tele.records:
            _LOGGER.warning("Zähler %s sendet verschlüsselt – bitte AES-Schlüssel eintragen", mid)
            return

        values = tele.values()
        if rssi is not None:
            values["rssi"] = rssi
        new_keys = [k for k in values if k not in self.meter_keys.get(mid, [])]
        if new_keys:
            self.meter_keys.setdefault(mid, []).extend(new_keys)
            self._save_later(1)
            async_dispatcher_send(self.hass, signal_meter_new(self.entry_id), mid, tele, new_keys, values)
        async_dispatcher_send(self.hass, signal_meter(self.entry_id, mid), values)

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

    # ------------------------------------------------------------------ Aufräumen
    async def async_forget_meter(self, meter_id: str) -> None:
        self.meter_keys.pop(meter_id, None)
        await self._store.async_save(self._store_data())
