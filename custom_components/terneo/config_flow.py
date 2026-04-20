"""Config & options flow for the Terneo integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    TerneoClient,
    TerneoConnectionError,
    TerneoError,
)
from .cloud import (
    TerneoCloudAuthError,
    TerneoCloudDeviceNotFoundError,
    TerneoCloudError,
    async_fetch_totp_key,
)
from .const import (
    CONF_POWER_WATTS,
    CONF_PRIMARY_SENSOR,
    CONF_SERIAL,
    CONF_TOTP_KEY,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DOMAIN,
    PRIMARY_AIR,
    PRIMARY_FLOOR,
)


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=d.get(CONF_HOST, "")): str,
            vol.Required(CONF_SERIAL, default=d.get(CONF_SERIAL, "")): str,
            vol.Optional(CONF_NAME, default=d.get(CONF_NAME, DEFAULT_NAME)): str,
            vol.Optional(CONF_PORT, default=d.get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Optional(CONF_TOTP_KEY, default=d.get(CONF_TOTP_KEY, "")): str,
            vol.Optional(
                CONF_POWER_WATTS, default=d.get(CONF_POWER_WATTS, 0)
            ): vol.All(int, vol.Range(min=0, max=20000)),
            vol.Optional(
                CONF_PRIMARY_SENSOR, default=d.get(CONF_PRIMARY_SENSOR, PRIMARY_AIR)
            ): vol.In([PRIMARY_AIR, PRIMARY_FLOOR]),
        }
    )


def _default_discovery_name(serial: str, hw: str | None) -> str:
    """Pick a friendly name from the broadcast payload."""
    suffix = serial[-4:] if serial else ""
    hw_label = (hw or "").upper() or "Thermostat"
    return f"Terneo {hw_label} {suffix}".strip()


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Drop empty-string optional fields."""
    return {
        k: v
        for k, v in user_input.items()
        if not (isinstance(v, str) and v == "")
    }


class TerneoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup of a Terneo device."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_host: str | None = None
        self._discovery_serial: str | None = None
        self._discovery_name: str | None = None
        self._pending_data: dict[str, Any] | None = None

    # --------------------------------------------------------------- manual

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            cleaned = _clean(user_input)
            serial = cleaned[CONF_SERIAL]
            await self.async_set_unique_id(serial)
            self._abort_if_unique_id_configured(
                updates={
                    CONF_HOST: cleaned[CONF_HOST],
                    CONF_PORT: cleaned.get(CONF_PORT, DEFAULT_PORT),
                }
            )

            result = await self._async_try_connect(cleaned)
            if result is not None:
                errors["base"] = result
            elif cleaned.get(CONF_TOTP_KEY):
                return self._create_entry(cleaned)
            else:
                self._pending_data = cleaned
                return await self.async_step_totp_method()

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
        )

    # ----------------------------------------------------------- totp choice

    async def async_step_totp_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose how to handle TOTP authentication."""
        return self.async_show_menu(
            step_id="totp_method",
            menu_options={
                "skip_totp": "Continue without TOTP (LAN lock is off)",
                "fetch_totp": "Fetch TOTP key from Terneo cloud account",
            },
        )

    async def async_step_skip_totp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Continue without a TOTP key."""
        assert self._pending_data is not None
        return self._create_entry(self._pending_data)

    async def async_step_fetch_totp(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fetch the TOTP key from the Terneo cloud account."""
        assert self._pending_data is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                key = await async_fetch_totp_key(
                    session,
                    email=user_input["email"],
                    password=user_input["password"],
                    serial=self._pending_data[CONF_SERIAL],
                )
            except TerneoCloudAuthError:
                errors["base"] = "cloud_auth_failed"
            except TerneoCloudDeviceNotFoundError:
                errors["base"] = "cloud_device_not_found"
            except TerneoCloudError:
                errors["base"] = "unknown"
            else:
                self._pending_data[CONF_TOTP_KEY] = key
                return self._create_entry(self._pending_data)

        return self.async_show_form(
            step_id="fetch_totp",
            data_schema=vol.Schema(
                {
                    vol.Required("email"): str,
                    vol.Required("password"): str,
                }
            ),
            errors=errors,
        )

    # -------------------------------------------------------- auto-discovery

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Entered automatically when discovery.py receives a broadcast."""
        serial = str(discovery_info.get("sn") or "").strip()
        host = str(discovery_info.get("host") or "").strip()
        hw = discovery_info.get("hw")

        if not serial or not host:
            return self.async_abort(reason="invalid_discovery")

        await self.async_set_unique_id(serial)
        # If the device is already configured, quietly refresh its IP
        # (broadcasts carry the current address) and abort the flow.
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        self._discovery_host = host
        self._discovery_serial = serial
        self._discovery_name = _default_discovery_name(serial, hw)

        # Show a nicer chip in the Settings → Devices UI
        self.context["title_placeholders"] = {"name": self._discovery_name}

        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to confirm adding the discovered device."""
        assert self._discovery_host and self._discovery_serial

        errors: dict[str, str] = {}
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_NAME,
                    default=self._discovery_name or DEFAULT_NAME,
                ): str,
                vol.Optional(CONF_TOTP_KEY, default=""): str,
                vol.Optional(CONF_POWER_WATTS, default=0): vol.All(
                    int, vol.Range(min=0, max=20000)
                ),
                vol.Optional(CONF_PRIMARY_SENSOR, default=PRIMARY_AIR): vol.In(
                    [PRIMARY_AIR, PRIMARY_FLOOR]
                ),
            }
        )

        if user_input is not None:
            cleaned = _clean(user_input)
            cleaned[CONF_HOST] = self._discovery_host
            cleaned[CONF_SERIAL] = self._discovery_serial
            cleaned.setdefault(CONF_PORT, DEFAULT_PORT)

            result = await self._async_try_connect(cleaned)
            if result is not None:
                errors["base"] = result
            elif cleaned.get(CONF_TOTP_KEY):
                return self._create_entry(cleaned)
            else:
                self._pending_data = cleaned
                return await self.async_step_totp_method()

        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=schema,
            description_placeholders={
                "name": self._discovery_name or DEFAULT_NAME,
                "host": self._discovery_host,
                "serial": self._discovery_serial,
            },
            errors=errors,
        )

    # ------------------------------------------------------------- helpers

    async def _async_try_connect(self, data: dict[str, Any]) -> str | None:
        """Probe the device. Return ``None`` on success or an error key."""
        session = async_get_clientsession(self.hass)
        client = TerneoClient(
            session,
            host=data[CONF_HOST],
            serial=data[CONF_SERIAL],
            port=data.get(CONF_PORT, DEFAULT_PORT),
        )
        try:
            await client.async_probe()
        except TerneoConnectionError:
            return "cannot_connect"
        except TerneoError:
            return "unknown"
        return None

    def _create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        entry_data: dict[str, Any] = {
            CONF_HOST: data[CONF_HOST],
            CONF_PORT: data.get(CONF_PORT, DEFAULT_PORT),
            CONF_SERIAL: data[CONF_SERIAL],
            CONF_NAME: data.get(CONF_NAME, DEFAULT_NAME),
        }
        if data.get(CONF_TOTP_KEY):
            entry_data[CONF_TOTP_KEY] = data[CONF_TOTP_KEY]
        return self.async_create_entry(
            title=data.get(CONF_NAME, DEFAULT_NAME),
            data=entry_data,
            options={
                CONF_POWER_WATTS: data.get(CONF_POWER_WATTS, 0),
                CONF_PRIMARY_SENSOR: data.get(CONF_PRIMARY_SENSOR, PRIMARY_AIR),
            },
        )

    # -------------------------------------------------------------- options

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return TerneoOptionsFlow()


class TerneoOptionsFlow(OptionsFlow):
    """Editable options after initial setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_POWER_WATTS, default=current.get(CONF_POWER_WATTS, 0)
                ): vol.All(int, vol.Range(min=0, max=20000)),
                vol.Optional(
                    CONF_PRIMARY_SENSOR,
                    default=current.get(CONF_PRIMARY_SENSOR, PRIMARY_AIR),
                ): vol.In([PRIMARY_AIR, PRIMARY_FLOOR]),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
