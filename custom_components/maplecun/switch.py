"""Schalter: Intertechno-Funksteckdosen (Tristate, a-culfw-Befehl "is")."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import MapleCunConfigEntry
from .const import CONF_IT_DEVICES, CONF_NAME, CONF_OFF, CONF_ON, DOMAIN, signal_connection


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MapleCunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Je konfigurierter Intertechno-Steckdose einen Schalter anlegen."""
    devices: dict[str, dict] = entry.options.get(CONF_IT_DEVICES, {})

    # Geräte entfernen, die in den Optionen gelöscht wurden
    dev_reg = dr.async_get(hass)
    wanted = {f"{entry.entry_id}_it_{code}" for code in devices}
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        for domain, ident in device.identifiers:
            if domain == DOMAIN and ident.startswith(f"{entry.entry_id}_it_") and ident not in wanted:
                dev_reg.async_remove_device(device.id)

    async_add_entities(IntertechnoSwitch(entry, code, dev) for code, dev in devices.items())


class IntertechnoSwitch(SwitchEntity, RestoreEntity):
    """Intertechno-Steckdose. Funk ist Einweg – der Zustand wird angenommen."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_assumed_state = True

    def __init__(self, entry: MapleCunConfigEntry, code: str, dev: dict) -> None:
        self._entry = entry
        self._hub = entry.runtime_data
        self._code = code
        self._on = dev[CONF_ON]
        self._off = dev[CONF_OFF]
        self._attr_unique_id = f"{entry.entry_id}_it_{code}"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_it_{code}")},
            name=dev.get(CONF_NAME) or f"Intertechno {code}",
            manufacturer="Intertechno",
            model=f"Tristate {code}",
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def available(self) -> bool:
        return self._hub.connected

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_connection(self._entry.entry_id), self.async_write_ha_state
            )
        )

    async def _send(self, suffix: str, state: bool) -> None:
        try:
            await self._hub.async_send(f"is{self._code}{suffix}")
        except (ConnectionError, OSError) as err:
            raise HomeAssistantError(f"Senden an MapleCUN fehlgeschlagen: {err}") from err
        self._attr_is_on = state
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send(self._on, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(self._off, False)
