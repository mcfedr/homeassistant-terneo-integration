"""The Terneo Thermostat integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api import TerneoClient
from .const import CONF_SERIAL, CONF_TOTP_KEY, DEFAULT_NAME, DEFAULT_PORT, DOMAIN
from .coordinator import TerneoCoordinator
from .discovery import async_start_listener

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SENSOR]

# Where we remember devices we've already announced during this HA run so
# we don't spam the config-flow manager with a new flow every 30 seconds.
_SEEN_SERIALS_KEY = "_seen_serials"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Start the UDP discovery listener once at HA startup.

    The listener runs regardless of whether any Terneo config entries exist,
    so first-time users see their device pop up as "Discovered" in Settings.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    seen_serials: set[str] = domain_data.setdefault(_SEEN_SERIALS_KEY, set())

    async def _on_device(payload: dict[str, Any], ip: str) -> None:
        serial = str(payload.get("sn") or "").strip()
        if not serial or serial in seen_serials:
            return
        seen_serials.add(serial)

        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
            data={
                "sn": serial,
                "host": ip,
                "hw": payload.get("hw"),
            },
        )

    transport = await async_start_listener(hass, _on_device)
    if transport is not None:
        async def _stop(_event: Event) -> None:
            transport.close()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
        domain_data["_discovery_transport"] = transport

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Terneo from a config entry."""
    session = async_get_clientsession(hass)
    client = TerneoClient(
        session,
        host=entry.data[CONF_HOST],
        serial=entry.data[CONF_SERIAL],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        totp_key=entry.data.get(CONF_TOTP_KEY),
    )

    coordinator = TerneoCoordinator(
        hass,
        client,
        name=entry.data.get(CONF_NAME, DEFAULT_NAME),
    )

    # First refresh. The coordinator translates connection/locked-API errors
    # to ``UpdateFailed``. ``async_config_entry_first_refresh`` converts that
    # into ``ConfigEntryNotReady`` on its own.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
