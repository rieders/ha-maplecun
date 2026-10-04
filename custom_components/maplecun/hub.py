"""Verbindung zum MapleCUN-Modul (über maplecun-splitproxy oder direkt)."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from . import revolt
from .const import (
    DOMAIN,
    RECONNECT_DELAY,
    STORAGE_VERSION,
    signal_connection,
    signal_raw,
    signal_revolt,
    signal_revolt_new,
)

_LOGGER = logging.getLogger(__name__)


class MapleCunHub:
    """Hält die TCP-Verbindung, dekodiert Funkzeilen und verteilt sie an die Entitäten."""

    def __init__(self, hass: HomeAssistant, entry_id: str, host: str, port: int) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.host = host
        self.port = port
        self.connected = False
        self.version: str | None = None
        self.known_revolts: set[str] = set()
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._last_energy: dict[str, tuple[float, float]] = {}
        self._send_lock = asyncio.Lock()

    # ------------------------------------------------------------------ Lebenszyklus
    async def async_start(self) -> None:
        """Gespeicherte Geräte laden und Verbindungsschleife starten."""
        data = await self._store.async_load() or {}
        self.known_revolts = set(data.get("revolts", []))
        self._task = self.hass.async_create_background_task(
            self._run(), f"{DOMAIN}_{self.entry_id}_connection"
        )

    async def async_stop(self) -> None:
        """Verbindung beenden."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close()

    async def _close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            self._writer = None
        self._set_connected(False)

    @callback
    def _set_connected(self, state: bool) -> None:
        if state != self.connected:
            self.connected = state
            async_dispatcher_send(self.hass, signal_connection(self.entry_id))

    async def _run(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=10
                )
                self._writer = writer
                self._set_connected(True)
                _LOGGER.info("MapleCUN verbunden: %s:%s", self.host, self.port)
                writer.write(b"V\r\n")
                await writer.drain()
                while True:
                    raw = await reader.readline()
                    if not raw:
                        raise ConnectionError("Verbindung vom Gegenüber geschlossen")
                    line = raw.decode(errors="replace").strip()
                    if line:
                        try:
                            self._handle_line(line)
                        except Exception:  # noqa: BLE001 – eine kaputte Zeile darf die Schleife nicht beenden
                            _LOGGER.exception("Fehler beim Verarbeiten von %r", line)
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionError, asyncio.TimeoutError) as err:
                if self.connected:
                    _LOGGER.warning("MapleCUN-Verbindung verloren: %s", err)
                else:
                    _LOGGER.debug("MapleCUN nicht erreichbar: %s", err)
            await self._close()
            await asyncio.sleep(RECONNECT_DELAY)

    # ------------------------------------------------------------------ Senden
    async def async_send(self, command: str) -> None:
        """Befehl an das Modul schicken."""
        async with self._send_lock:
            if not self._writer or not self.connected:
                raise ConnectionError("MapleCUN ist nicht verbunden")
            _LOGGER.debug("-> %s", command)
            self._writer.write(command.encode() + b"\r\n")
            await self._writer.drain()

    # ------------------------------------------------------------------ Empfangen
    @callback
    def _handle_line(self, line: str) -> None:
        if line.startswith("V ") and "culfw" in line:
            self.version = line
            _LOGGER.debug("Firmware: %s", line)
            return

        if revolt.is_revolt(line):
            self._handle_revolt(line)
            return

        async_dispatcher_send(self.hass, signal_raw(self.entry_id), line)

    @callback
    def _handle_revolt(self, line: str) -> None:
        rid, values = revolt.decode(line)
        if not revolt.plausible(values):
            _LOGGER.debug("Revolt %s unplausibel, verworfen: %s", rid, line)
            return

        now = time.monotonic()
        last = self._last_energy.get(rid)
        if last:
            e0, t0 = last
            if values["energy"] - e0 > revolt.MAX_KW * (now - t0) / 3600 + 0.02:
                _LOGGER.debug("Revolt %s Energiesprung verworfen: %s -> %s", rid, e0, values["energy"])
                values["energy"] = e0
            else:
                self._last_energy[rid] = (values["energy"], now)
        else:
            self._last_energy[rid] = (values["energy"], now)

        if rid not in self.known_revolts:
            _LOGGER.info("Neuer Revolt-Energiemesser entdeckt: %s", rid)
            self.known_revolts.add(rid)
            self._store.async_delay_save(
                lambda: {"revolts": sorted(self.known_revolts)}, 1
            )
            # Erste Messwerte gleich mitgeben, damit sie nicht verloren gehen
            async_dispatcher_send(self.hass, signal_revolt_new(self.entry_id), rid, values)
            return

        async_dispatcher_send(self.hass, signal_revolt(self.entry_id, rid), values)

    async def async_forget_revolt(self, rid: str) -> None:
        """Revolt aus der gespeicherten Liste entfernen."""
        self.known_revolts.discard(rid)
        self._last_energy.pop(rid, None)
        await self._store.async_save({"revolts": sorted(self.known_revolts)})
