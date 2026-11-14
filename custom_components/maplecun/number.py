"""Einstellungen als Zahl: Frequenz (EEPROM) und Intertechno-Pulslänge (RAM).

Wie FHEM "set freq" / "set ITClock"; nur im 433-MHz-Betrieb, standardmäßig deaktiviert,
geschrieben wird ausschließlich nach einer Nutzeraktion (nie beim Start).
"""

from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import EntityCategory, UnitOfFrequency, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MapleCunConfigEntry
from .const import DOMAIN, ROLE_RF433, signal_connection, signal_module_info
from .naming import with_fallback_name


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MapleCunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    entities = []
    for mid, mod in sorted(hub.modules.items()):
        if mod.role != ROLE_RF433:
            continue              # wMBus: Firmware lädt eigene Funkeinstellungen
        entities.append(FreqNumber(entry, mid))
        if not mod.cmds or "i" in mod.cmds:
            entities.append(ITClockNumber(entry, mid))
    async_add_entities(entities)


class _Base(RestoreNumber):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: MapleCunConfigEntry, module_id: str) -> None:
        self._entry = entry
        self._hub = entry.runtime_data
        self._mid = module_id
        self._attr_translation_placeholders = {"module": module_id}
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        with_fallback_name(self, "number", NumberEntityDescription)

    @property
    def available(self) -> bool:
        return self._hub.is_connected(self._mid) and self._hub.can_configure_radio(self._mid)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for sig in (signal_connection, signal_module_info):
            self.async_on_remove(async_dispatcher_connect(self.hass, sig(self._entry.entry_id), self.async_write_ha_state))


class FreqNumber(_Base):
    """Frequenz (FREQ2..0, EEPROM). Zustand = gelesener Wert aus ccconf."""

    _attr_translation_key = "set_freq"
    _attr_device_class = NumberDeviceClass.FREQUENCY
    _attr_native_unit_of_measurement = UnitOfFrequency.MEGAHERTZ
    _attr_native_step = 0.001

    def __init__(self, entry, module_id: str) -> None:
        super().__init__(entry, module_id)
        self._attr_unique_id = f"{entry.entry_id}_set_freq_{module_id}"

    @property
    def _current(self) -> float | None:
        return self._hub.modules[self._mid].info.get("freq")

    @property
    def native_min_value(self) -> float:
        # Antenne ist auf ein Band abgestimmt: Bereich des CC1101 um die aktuelle Frequenz
        return 779.0 if (self._current or 433) > 600 else 387.0

    @property
    def native_max_value(self) -> float:
        return 928.0 if (self._current or 433) > 600 else 464.0

    @property
    def native_value(self) -> float | None:
        return self._current

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._hub.async_write_radio(self._mid, "freq", value)
        except (ValueError, ConnectionError, OSError) as err:
            raise HomeAssistantError(str(err)) from err


class ITClockNumber(_Base):
    """Intertechno-Pulslänge in µs ("ic", nur im RAM des MapleCUN; FHEM-Vorgabe 250)."""

    _attr_translation_key = "set_itclock"
    _attr_native_unit_of_measurement = UnitOfTime.MICROSECONDS
    _attr_native_min_value = 100
    _attr_native_max_value = 1000
    _attr_native_step = 1

    def __init__(self, entry, module_id: str) -> None:
        super().__init__(entry, module_id)
        self._attr_unique_id = f"{entry.entry_id}_set_itclock_{module_id}"
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Nur den zuletzt gesetzten Wert anzeigen – NICHT erneut senden
        if (last := await self.async_get_last_number_data()) is not None:
            self._attr_native_value = last.native_value

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._hub.async_set_itclock(self._mid, int(value))
        except (ValueError, ConnectionError, OSError) as err:
            raise HomeAssistantError(str(err)) from err
        self._attr_native_value = int(value)
        self.async_write_ha_state()
