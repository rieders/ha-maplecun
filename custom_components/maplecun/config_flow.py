"""Einrichtung und Optionen der MapleCUN-Integration."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_CODE,
    CONF_IT_CODES,
    CONF_IT_DEVICES,
    CONF_NAME,
    CONF_OFF,
    CONF_ON,
    DEFAULT_IT_OFF,
    DEFAULT_IT_ON,
    DEFAULT_PORT,
    DOMAIN,
)

_CODE_RE = re.compile(r"^[0F1D]{10}$")
_CMD_RE = re.compile(r"^[0F1D]{2}$")


async def _probe(host: str, port: int) -> str | None:
    """Verbindung testen. Liefert die Firmware-Version oder None (verbunden, aber keine Antwort)."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
    try:
        writer.write(b"V\r\n")
        await writer.drain()
        end = asyncio.get_running_loop().time() + 3
        while (left := end - asyncio.get_running_loop().time()) > 0:
            raw = await asyncio.wait_for(reader.readline(), timeout=left)
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if line.startswith("V ") and "culfw" in line:
                return line
    except asyncio.TimeoutError:
        pass
    finally:
        writer.close()
    return None


def _parse_codes(text: str) -> tuple[dict[str, dict], list[str]]:
    """'FFFF0F0FFF, FFFF00FFFF' -> {code: {...}}, ungültige Codes."""
    devices: dict[str, dict] = {}
    invalid: list[str] = []
    for code in re.split(r"[\s,;]+", text.strip().upper()):
        if not code:
            continue
        if _CODE_RE.match(code):
            devices[code] = {CONF_NAME: f"Intertechno {code}", CONF_ON: DEFAULT_IT_ON, CONF_OFF: DEFAULT_IT_OFF}
        else:
            invalid.append(code)
    return devices, invalid


class MapleCunConfigFlow(ConfigFlow, domain=DOMAIN):
    """Einrichtung über die Oberfläche."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            devices, invalid = _parse_codes(user_input.get(CONF_IT_CODES, ""))
            if invalid:
                errors[CONF_IT_CODES] = "invalid_code"
            else:
                try:
                    version = await _probe(host, port)
                except (OSError, asyncio.TimeoutError):
                    errors["base"] = "cannot_connect"
                else:
                    title = f"MapleCUN {host}:{port}"
                    return self.async_create_entry(
                        title=title,
                        data={CONF_HOST: host, CONF_PORT: port},
                        options={CONF_IT_DEVICES: devices},
                        description_placeholders={"version": version or "?"},
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)): cv.port,
                vol.Optional(CONF_IT_CODES, default=(user_input or {}).get(CONF_IT_CODES, "")): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> MapleCunOptionsFlow:
        return MapleCunOptionsFlow()


class MapleCunOptionsFlow(OptionsFlow):
    """Intertechno-Steckdosen hinzufügen / entfernen."""

    def _devices(self) -> dict[str, dict]:
        return dict(self.config_entry.options.get(CONF_IT_DEVICES, {}))

    def _save(self, devices: dict[str, dict]) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, CONF_IT_DEVICES: devices})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        menu = ["add_it"]
        if self._devices():
            menu += ["edit_it", "remove_it"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_add_it(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_CODE].strip().upper()
            on = user_input[CONF_ON].strip().upper()
            off = user_input[CONF_OFF].strip().upper()
            devices = self._devices()
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
                return self._save(devices)

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
        """Gerät auswählen, dann Ein-/Aus-Codes ändern."""
        devices = self._devices()
        if user_input is not None:
            self._edit_code = user_input[CONF_CODE]
            return await self.async_step_edit_it_codes()
        schema = vol.Schema(
            {vol.Required(CONF_CODE): vol.In({c: f"{d[CONF_NAME]} ({c})" for c, d in devices.items()})}
        )
        return self.async_show_form(step_id="edit_it", data_schema=schema)

    async def async_step_edit_it_codes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        devices = self._devices()
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
                return self._save(devices)
        schema = vol.Schema(
            {
                vol.Required(CONF_ON, default=dev[CONF_ON]): str,
                vol.Required(CONF_OFF, default=dev[CONF_OFF]): str,
            }
        )
        return self.async_show_form(
            step_id="edit_it_codes",
            data_schema=schema,
            errors=errors,
            description_placeholders={"code": self._edit_code},
        )

    async def async_step_remove_it(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        devices = self._devices()
        if user_input is not None:
            for code in user_input.get("codes", []):
                devices.pop(code, None)
            return self._save(devices)
        schema = vol.Schema(
            {
                vol.Optional("codes", default=[]): cv.multi_select(
                    {c: f"{d[CONF_NAME]} ({c})" for c, d in devices.items()}
                )
            }
        )
        return self.async_show_form(step_id="remove_it", data_schema=schema)
