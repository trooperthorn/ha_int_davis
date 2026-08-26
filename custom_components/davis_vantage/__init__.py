"""The Davis Vantage integration."""

from __future__ import annotations
from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers import issue_registry as ir
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady

from .client import DavisVantageClient
from .const import (
    DOMAIN,
    NAME,
    MANUFACTURER,
    CONFIG_STATION_MODEL,
    CONFIG_INTERVAL,
    CONFIG_PROTOCOL,
    CONFIG_LINK,
    CONFIG_PERSISTENT_CONNECTION,
    CONFIG_BAUD_RATE,
    DEFAULT_BAUD_RATE,
)
from .coordinator import DavisVantageDataUpdateCoordinator
from .services import DavisServicesSetup

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.WEATHER]

# Suppress pyvpdriver / serial library verbose output
logging.getLogger("pyvpdriver").setLevel(logging.WARNING)

_LOGGER = logging.getLogger(__name__)


@dataclass
class RuntimeData:
    """Class to hold runtime integration data."""
    coordinator: DavisVantageDataUpdateCoordinator


type DavisConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(
    hass: HomeAssistant, config_entry: DavisConfigEntry
) -> bool:
    """Set up Davis Vantage from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    _LOGGER.debug("Setting up entry %s with data: %s", config_entry.entry_id, config_entry.data)
    _LOGGER.debug("Entry options: %s", config_entry.options)

    # 1. Connection configuration
    protocol = config_entry.data.get(CONFIG_PROTOCOL, "")
    link = config_entry.data.get(CONFIG_LINK, "")
    
    persistent_connection = config_entry.options.get(
        CONFIG_PERSISTENT_CONNECTION, 
        config_entry.data.get(CONFIG_PERSISTENT_CONNECTION, False)
    )

    # 2. Client instantiation
    # Handles both signature variations (with or without use_loop2 parameter)
    baud_rate = config_entry.options.get(
        CONFIG_BAUD_RATE,
        config_entry.data.get(CONFIG_BAUD_RATE, DEFAULT_BAUD_RATE),
    )

    try:
        use_loop2 = config_entry.options.get(
            "use_loop2",
            config_entry.data.get("use_loop2", False)
        )
        client = DavisVantageClient(
            hass,
            protocol,
            link,
            persistent_connection,
            use_loop2=use_loop2,
            baud_rate=baud_rate,
        )
    except TypeError:
        # Fallback if custom DavisVantageClient does not accept use_loop2 in __init__
        client = DavisVantageClient(hass, protocol, link, persistent_connection)

    # 3. Verify hardware connectivity
    try:
        await client.connect_to_station()
        await client.get_station_info()
    except Exception as err:
        _LOGGER.warning(
            "Could not connect to Davis Vantage console at %s (%s). Will retry in background.",
            link,
            err,
        )
        raise ConfigEntryNotReady(f"Failed to connect to Davis station: {err}") from err

    # 4. Device Registry Information
    device_info = DeviceInfo(
        identifiers={(DOMAIN, config_entry.entry_id)},
        manufacturer=MANUFACTURER,
        name=NAME,
        model=config_entry.data.get(CONFIG_STATION_MODEL, "Davis Vantage Weather Station"),
        sw_version=getattr(client, "firmware_version", None),
        hw_version=None,
    )

    # 5. Initialize Coordinator
    coordinator = DavisVantageDataUpdateCoordinator(
        hass=hass, 
        client=client, 
        device_info=device_info, 
        config_entry=config_entry
    )

    # Initial data refresh
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning("Initial refresh failed for Davis Vantage (%s). Retrying...", err)
        # Ensure the connection is closed before failing setup, so HA's
        # automatic retry of ConfigEntryNotReady doesn't hit "Resource busy"
        # trying to reopen a port this entry never released.
        await client.async_close()
        raise ConfigEntryNotReady(f"Initial data fetch failed: {err}") from err

    # 6. Store references
    config_entry.runtime_data = RuntimeData(coordinator=coordinator)
    hass.data[DOMAIN][config_entry.entry_id] = coordinator

    # 7. Forward setup to sensor, binary_sensor, and weather platforms
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # 8. Register entry update listener (Configure / Options Flow)
    config_entry.async_on_unload(
        config_entry.add_update_listener(async_reload_entry)
    )

    # 9. Register Services / Actions
    # DavisServicesSetup.__init__ calls hass.services.register() (the
    # thread-safe sync variant, per services.py) - run it off the event loop.
    await hass.async_add_executor_job(DavisServicesSetup, hass, config_entry)

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: DavisConfigEntry) -> bool:
    """Unload a config entry and release serial/network resources."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )

    if unload_ok:
        coordinator = config_entry.runtime_data.coordinator
        client = coordinator.client

        # Release the serial/TCP connection to prevent 'Resource busy' errors on reload.
        # async_close() only touches the connection if one was actually opened -
        # it never triggers the `link` property's lazy-connect behavior.
        try:
            await client.async_close()
        except Exception as err:
            _LOGGER.error("Error closing Davis station connection during unload: %s", err)

        # Don't leave a stale "connection lost" repair issue behind if the
        # user removes the integration while one is open.
        ir.async_delete_issue(hass, DOMAIN, "connection_lost")

        # Clean up legacy dictionary reference
        hass.data[DOMAIN].pop(config_entry.entry_id, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, config_entry: DavisConfigEntry) -> None:
    """Reload config entry when options are updated."""
    await hass.config_entries.async_reload(config_entry.entry_id)