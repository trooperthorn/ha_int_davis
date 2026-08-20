"""Diagnostics support for Davis Vantage.

Surfaces the console's own diagnostics report (RXCHECK), firmware version
(NVER), and barometer calibration (BARDATA) - all previously wired up in
client.py but never exposed anywhere - alongside the last polled data, via
Home Assistant's built-in "Download diagnostics" feature (Settings >
Devices & services > this integration > the failed/ok entry > Download
diagnostics).
"""
from __future__ import annotations

from typing import Any

from collections.abc import Awaitable, Callable

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import DavisConfigEntry

# Station latitude/longitude are personal location data - redact them from
# any diagnostics dump a user might attach to a public GitHub issue.
TO_REDACT = {"Latitude", "Longitude", "latitude", "longitude"}


async def _try_console_command(func: Callable[[], Awaitable[str]]) -> str:
    """Run a console round-trip, degrading to an error string on failure.

    Diagnostics are frequently downloaded *because* the console is
    unreachable - a raised exception here would take out the whole
    diagnostics payload (config, last polled data, raw bytes) along with
    it, when all of that is still useful without the live console replies.
    """
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

    raw_data = dict(client.get_raw_data())
    raw_data.pop("_raw_bytes", None)

    return {
        "config_entry": {
            "protocol": config_entry.data.get("protocol"),
            "use_loop2": config_entry.options.get(
                "use_loop2", config_entry.data.get("use_loop2")
            ),
            "baud_rate": config_entry.options.get(
                "baud_rate", config_entry.data.get("baud_rate")
            ),
            "interval": config_entry.options.get(
                "interval", config_entry.data.get("interval")
            ),
            "persistent_connection": config_entry.options.get(
                "persistent_connection",
                config_entry.data.get("persistent_connection"),
            ),
        },
        "console": {
            "firmware_version": client.firmware_version,
            "rxcheck": rxcheck,
            "nver": nver,
            "bardata": bardata,
        },
        "last_data": async_redact_data(dict(coordinator.data or {}), TO_REDACT),
        "raw_loop_data": raw_data,
    }
