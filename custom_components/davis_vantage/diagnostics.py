"""Diagnostics support for Davis Vantage."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import DavisConfigEntry
from .const import (
    CONF_USE_LOOP2,
    CONFIG_BAUD_RATE,
    CONFIG_IDENTITY_SOURCE,
    CONFIG_IDENTITY_STRENGTH,
    CONFIG_INTERVAL,
    CONFIG_LOOP2_SUPPORTED,
    CONFIG_PERSISTENT_CONNECTION,
    CONFIG_PROTOCOL,
)

# Latitude/longitude are personal location data; keep them out of shared dumps.
TO_REDACT = {
    "Latitude",
    "Longitude",
    "latitude",
    "longitude",
    "EEPROM",
    "eeprom",
    "raw_eeprom",
}


async def _try_console_command(func: Callable[[], Awaitable[str]]) -> str:
    """Run a console round-trip, degrading to an error string on failure."""
    try:
        return (await func()).strip()
    except Exception as err:
        return f"unavailable: {err}"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: DavisConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = config_entry.runtime_data.coordinator
    client = coordinator.client

    rxcheck = await _try_console_command(client.async_get_rxcheck)
    nver = await _try_console_command(client.async_get_nver)
    bardata = await _try_console_command(client.async_get_bardata)
    try:
        station_type = str(await client.async_get_station_type())
    except Exception as err:
        station_type = f"unavailable: {err}"
    try:
        receivers = f"{await client.async_get_receivers():#010b}"
    except Exception as err:
        receivers = f"unavailable: {err}"

    raw_data = dict(client.get_raw_data())
    raw_data.pop("_raw_bytes", None)
    connection = dict(getattr(client, "connection_diagnostics", {}) or {})
    connection["coordinator_failure_streak"] = getattr(
        coordinator, "consecutive_failures", getattr(coordinator, "_consecutive_failures", 0)
    )

    return {
        "config_entry": {
            "protocol": config_entry.data.get(CONFIG_PROTOCOL),
            "identity_source": config_entry.data.get(CONFIG_IDENTITY_SOURCE),
            "identity_strength": config_entry.data.get(CONFIG_IDENTITY_STRENGTH),
            "loop2_supported": config_entry.data.get(CONFIG_LOOP2_SUPPORTED),
            "use_loop2": config_entry.options.get(CONF_USE_LOOP2, False),
            "baud_rate": config_entry.data.get(CONFIG_BAUD_RATE),
            "interval": config_entry.options.get(CONFIG_INTERVAL),
            "persistent_connection": config_entry.options.get(
                CONFIG_PERSISTENT_CONNECTION, False
            ),
        },
        "connection": connection,
        "console": {
            "firmware_version": client.firmware_version,
            "rxcheck": rxcheck,
            "nver": nver,
            "bardata": bardata,
            "station_type": station_type,
            "receivers": receivers,
        },
        "last_data": async_redact_data(dict(coordinator.data or {}), TO_REDACT),
        "raw_loop_data": raw_data,
    }
