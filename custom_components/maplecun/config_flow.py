"""Einrichtung und Optionen der MapleCUN-Integration."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_AUTO,
    CONF_CODE,
    CONF_INIT,
    CONF_IT_CODES,
    CONF_IT_DEVICES,
    CONF_KEY,
    CONF_METER_ID,
    CONF_METERS,
    CONF_MODULES,
    CONF_NAME,
    CONF_OFF,
    CONF_ON,
    CONF_ROLE,
    DEFAULT_IT_OFF,
    DEFAULT_IT_ON,
    DEFAULT_AUTO,
    DEFAULT_MODULES,
    DOMAIN,
    KIND_IT,
    KIND_METER,
    KIND_REVOLT,
    KIND_SENSOR,
    MODULE_IDS,
    ROLE_OFF,
    ROLES,
)

_CODE_RE = re.compile(r"^[0F1D]{10}$")
_CMD_RE = re.compile(r"^[0F1D]{2}$")
_METER_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
_KEY_RE = re.compile(r"^[0-9A-Fa-f]{32}$")

ROLE_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=list(ROLES), translation_key="role", mode=SelectSelectorMode.DROPDOWN)
)


async def _probe(host: str, port: int) -> bool:
    """Lässt sich der Port öffnen?"""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


def _parse_codes(text: str) -> tuple[dict[str, dict], list[str]]:
    devices: dict[str, dict] = {}
    invalid: list[str] = []
    for code in re.split(r"[\s,;]+", (text or "").strip().upper()):
        if not code:
            continue
        if _CODE_RE.match(code):
            devices[code] = {CONF_NAME: f"Intertechno {code}", CONF_ON: DEFAULT_IT_ON, CONF_OFF: DEFAULT_IT_OFF}
        else:
            invalid.append(code)
    return devices, invalid


def _module_schema(modules: dict) -> dict:
    fields: dict = {}
    for mid in MODULE_IDS:
        cfg = modules.get(mid, DEFAULT_MODULES[mid])
        fields[vol.Required(f"port_{mid}", default=int(cfg["port"]))] = cv.port
        fields[vol.Required(f"role_{mid}", default=cfg[CONF_ROLE])] = ROLE_SELECTOR
    return fields


def _modules_from_input(user_input: dict) -> dict:
    return {
        mid: {"port": int(user_input[f"port_{mid}"]), CONF_ROLE: user_input[f"role_{mid}"]}
        for mid in MODULE_IDS
    }


async def _check_modules(host: str, modules: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    active = {mid: cfg for mid, cfg in modules.items() if cfg[CONF_ROLE] != ROLE_OFF}
    if not active:
        errors["base"] = "no_module"
        return errors
    results = await asyncio.gather(*(_probe(host, cfg["port"]) for cfg in active.values()))
    for mid, ok in zip(active, results):
        if not ok:
            errors[f"port_{mid}"] = "cannot_connect"
    return errors


class MapleCunConfigFlow(ConfigFlow, domain=DOMAIN):
    """Einrichtung über die Oberfläche."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()
            modules = _modules_from_input(user_input)
            devices, invalid = _parse_codes(user_input.get(CONF_IT_CODES, ""))
            if invalid:
                errors[CONF_IT_CODES] = "invalid_code"
            else:
                errors = await _check_modules(host, modules)
            if not errors:
                return self.async_create_entry(
                    title=f"MapleCUN {host}",
                    data={CONF_HOST: host, CONF_MODULES: modules},
                    options={
                        CONF_INIT: user_input.get(CONF_INIT, True),
                        CONF_IT_DEVICES: devices,
                        CONF_METERS: {},
                    },
                )

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                **_module_schema(_modules_from_input(defaults) if user_input else DEFAULT_MODULES),
                vol.Optional(CONF_INIT, default=defaults.get(CONF_INIT, True)): bool,
                vol.Optional(CONF_IT_CODES, default=defaults.get(CONF_IT_CODES, "")): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> MapleCunOptionsFlow:
        return MapleCunOptionsFlow()


class MapleCunOptionsFlow(OptionsFlow):
    """Module, Zähler und Intertechno-Steckdosen verwalten."""

    _edit_code: str
    _edit_meter: str

    # ------------------------------------------------------------------ Hilfen
    def _opt(self, key: str) -> dict:
        return dict(self.config_entry.options.get(key, {}))

    def _save(self, **changes) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    def _hub(self):
        return getattr(self.config_entry, "runtime_data", None)

    # ------------------------------------------------------------------ Menü
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub = self._hub()
        found = len(hub.discovered) if hub else 0
        menu = []
        if found:
            menu.append("discovered")
        menu += ["modules", "settings", "add_meter"]
        if self._opt(CONF_METERS):
            menu += ["edit_meter", "remove_meter"]
        menu.append("add_it")
        if self._opt(CONF_IT_DEVICES):
            menu += ["edit_it", "remove_it"]
        return self.async_show_menu(
            step_id="init", menu_options=menu, description_placeholders={"found": str(found)}
        )

    # ------------------------------------------------------------------ Gefundene Geräte
    async def async_step_discovered(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub = self._hub()
        if hub is None or not hub.discovered:
            return self.async_abort(reason="nothing_found")
        if user_input is not None:
            adopt = list(user_input.get("adopt", []))
            if user_input.get("ignore_rest"):
                await hub.async_ignore([k for k in hub.discovered if k not in adopt])
            return self.async_create_entry(data=hub.adopt_options(adopt))
        keys = hub.discovered_sorted()
        schema = vol.Schema(
            {
                vol.Optional("adopt", default=[]): cv.multi_select({k: hub.discovered_label(k) for k in keys}),
                vol.Optional("ignore_rest", default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="discovered", data_schema=schema, description_placeholders={"found": str(len(keys))}
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        auto = {**DEFAULT_AUTO, **self._opt(CONF_AUTO)}
        if user_input is not None:
            if user_input.get("reset_ignored") and (hub := self._hub()):
                await hub.async_reset_ignored()
            return self._save(**{CONF_AUTO: {
                KIND_REVOLT: user_input["auto_revolt"],
                KIND_IT: user_input["auto_it"],
                KIND_METER: user_input["auto_meter"],
                KIND_SENSOR: user_input["auto_sensor"],
            }})
        hub = self._hub()
        schema = vol.Schema(
            {
                vol.Required("auto_revolt", default=auto[KIND_REVOLT]): bool,
                vol.Required("auto_it", default=auto[KIND_IT]): bool,
                vol.Required("auto_sensor", default=auto[KIND_SENSOR]): bool,
                vol.Required("auto_meter", default=auto[KIND_METER]): bool,
                vol.Optional("reset_ignored", default=False): bool,
            }
        )
        return self.async_show_form(
            step_id="settings", data_schema=schema,
            description_placeholders={"ignored": str(len(hub.ignored) if hub else 0)},
        )

    # ------------------------------------------------------------------ Module
    async def async_step_modules(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        host = self.config_entry.data[CONF_HOST]
        if user_input is not None:
            modules = _modules_from_input(user_input)
            errors = await _check_modules(host, modules)
            if not errors:
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data={**self.config_entry.data, CONF_MODULES: modules}
                )
                return self._save(**{CONF_INIT: user_input.get(CONF_INIT, True)})
        current = _modules_from_input(user_input) if user_input else self.config_entry.data[CONF_MODULES]
        schema = vol.Schema(
            {
                **_module_schema(current),
                vol.Optional(CONF_INIT, default=self.config_entry.options.get(CONF_INIT, True)): bool,
            }
        )
        return self.async_show_form(
            step_id="modules", data_schema=schema, errors=errors, description_placeholders={"host": host}
        )

    # ------------------------------------------------------------------ Zähler
    async def async_step_add_meter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        meters = self._opt(CONF_METERS)
        if user_input is not None:
            meter_id = str(user_input[CONF_METER_ID]).strip().split(" ")[0].lower()
            key = (user_input.get(CONF_KEY) or "").strip().replace(" ", "")
            if not _METER_RE.match(meter_id):
                errors[CONF_METER_ID] = "invalid_meter"
            elif meter_id in meters:
                errors[CONF_METER_ID] = "already_exists"
            elif key and not _KEY_RE.match(key):
                errors[CONF_KEY] = "invalid_key"
            else:
                meters[meter_id] = {CONF_NAME: user_input.get(CONF_NAME, "").strip(), CONF_KEY: key.upper()}
                return self._save(**{CONF_METERS: meters})

        hub = getattr(self.config_entry, "runtime_data", None)
        seen: dict = hub.seen if hub is not None else {}
        rows = sorted(seen.items(), key=lambda kv: (kv[1].get("rssi") is None, -(kv[1].get("rssi") or -999)))
        options = [
            SelectOptionDict(
                value=mid,
                label=f"{mid} – {info.get('manufacturer', '?')} {info.get('type', '')}"
                + (f", {info['rssi']} dBm" if info.get("rssi") is not None else "")
                + (", verschlüsselt" if info.get("encrypted") else "")
                + (f", über Repeater {info['via']}" if info.get("via") else ""),
            )
            for mid, info in rows
            if mid not in meters
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_METER_ID): SelectSelector(
                    SelectSelectorConfig(options=options, custom_value=True, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional(CONF_NAME, default=""): str,
                vol.Optional(CONF_KEY, default=""): str,
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="add_meter", data_schema=schema, errors=errors,
            description_placeholders={"count": str(len(options))},
        )

    async def async_step_edit_meter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        meters = self._opt(CONF_METERS)
        if user_input is not None:
            self._edit_meter = user_input[CONF_METER_ID]
            return await self.async_step_edit_meter_details()
        schema = vol.Schema(
            {vol.Required(CONF_METER_ID): vol.In({m: f"{c.get(CONF_NAME) or m} ({m})" for m, c in meters.items()})}
        )
        return self.async_show_form(step_id="edit_meter", data_schema=schema)

    async def async_step_edit_meter_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        meters = self._opt(CONF_METERS)
        cfg = meters[self._edit_meter]
        errors: dict[str, str] = {}
        if user_input is not None:
            key = (user_input.get(CONF_KEY) or "").strip().replace(" ", "")
            if key and not _KEY_RE.match(key):
                errors[CONF_KEY] = "invalid_key"
            else:
                meters[self._edit_meter] = {CONF_NAME: user_input.get(CONF_NAME, "").strip(), CONF_KEY: key.upper()}
                return self._save(**{CONF_METERS: meters})
        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=cfg.get(CONF_NAME, "")): str,
                vol.Optional(CONF_KEY, default=cfg.get(CONF_KEY, "")): str,
            }
        )
        return self.async_show_form(
            step_id="edit_meter_details", data_schema=schema, errors=errors,
            description_placeholders={"meter": self._edit_meter},
        )

    async def async_step_remove_meter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        meters = self._opt(CONF_METERS)
        if user_input is not None:
            hub = getattr(self.config_entry, "runtime_data", None)
            for mid in user_input.get("meters", []):
                meters.pop(mid, None)
                if hub is not None:
                    await hub.async_forget_meter(mid)
            return self._save(**{CONF_METERS: meters})
        schema = vol.Schema(
            {
                vol.Optional("meters", default=[]): cv.multi_select(
                    {mid: f"{cfg.get(CONF_NAME) or mid} ({mid})" for mid, cfg in meters.items()}
                )
            }
        )
        return self.async_show_form(step_id="remove_meter", data_schema=schema)

    # ------------------------------------------------------------------ Intertechno
    async def async_step_add_it(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_CODE].strip().upper()
            on = user_input[CONF_ON].strip().upper()
            off = user_input[CONF_OFF].strip().upper()
            devices = self._opt(CONF_IT_DEVICES)
            if not _CODE_RE.match(code):
                errors[CONF_CODE] = "invalid_code"
            elif code in devices:
                errors[CONF_CODE] = "already_exists"
            elif not _CMD_RE.match(on):
                errors[CONF_ON] = "invalid_cmd"
            elif not _CMD_RE.match(off):
                errors[CONF_OFF] = "invalid_cmd"
            else:
                devices[code] = {
                    CONF_NAME: user_input.get(CONF_NAME) or f"Intertechno {code}",
                    CONF_ON: on,
                    CONF_OFF: off,
                }
                return self._save(**{CONF_IT_DEVICES: devices})
        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=""): str,
                vol.Required(CONF_CODE): str,
                vol.Required(CONF_ON, default=DEFAULT_IT_ON): str,
                vol.Required(CONF_OFF, default=DEFAULT_IT_OFF): str,
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="add_it", data_schema=schema, errors=errors)

    async def async_step_edit_it(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        devices = self._opt(CONF_IT_DEVICES)
        if user_input is not None:
            self._edit_code = user_input[CONF_CODE]
            return await self.async_step_edit_it_codes()
        schema = vol.Schema(
            {vol.Required(CONF_CODE): vol.In({c: f"{d[CONF_NAME]} ({c})" for c, d in devices.items()})}
        )
        return self.async_show_form(step_id="edit_it", data_schema=schema)

    async def async_step_edit_it_codes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        devices = self._opt(CONF_IT_DEVICES)
        dev = devices[self._edit_code]
        errors: dict[str, str] = {}
        if user_input is not None:
            on = user_input[CONF_ON].strip().upper()
            off = user_input[CONF_OFF].strip().upper()
            if not _CMD_RE.match(on):
                errors[CONF_ON] = "invalid_cmd"
            elif not _CMD_RE.match(off):
                errors[CONF_OFF] = "invalid_cmd"
            else:
                devices[self._edit_code] = {**dev, CONF_ON: on, CONF_OFF: off}
                return self._save(**{CONF_IT_DEVICES: devices})
        schema = vol.Schema(
            {
                vol.Required(CONF_ON, default=dev[CONF_ON]): str,
                vol.Required(CONF_OFF, default=dev[CONF_OFF]): str,
            }
        )
        return self.async_show_form(
            step_id="edit_it_codes", data_schema=schema, errors=errors,
            description_placeholders={"code": self._edit_code},
        )

    async def async_step_remove_it(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        devices = self._opt(CONF_IT_DEVICES)
        if user_input is not None:
            for code in user_input.get("codes", []):
                devices.pop(code, None)
            return self._save(**{CONF_IT_DEVICES: devices})
        schema = vol.Schema(
            {
                vol.Optional("codes", default=[]): cv.multi_select(
                    {c: f"{d[CONF_NAME]} ({c})" for c, d in devices.items()}
                )
            }
        )
        return self.async_show_form(step_id="remove_it", data_schema=schema)
