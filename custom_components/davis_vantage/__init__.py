"""The Davis Vantage integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo

from .client import DavisVantageClient
from .const import (
    CONF_USE_LOOP2,
    CONFIG_BAUD_RATE,
    CONFIG_IDENTITY,
    CONFIG_IDENTITY_SOURCE,
    CONFIG_IDENTITY_STRENGTH,
    CONFIG_INTERVAL,
    CONFIG_LINK,
    CONFIG_LOOP2_SUPPORTED,
    CONFIG_PERSISTENT_CONNECTION,
    CONFIG_PROTOCOL,
    CONFIG_STATION_MODEL,
    DEFAULT_BAUD_RATE,
    DOMAIN,
    IDENTITY_STRONG,
    MANUFACTURER,
    NAME,
)
from .coordinator import DavisVantageDataUpdateCoordinator
from .services import async_setup_services
from .verification import default_verification_result

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.WEATHER]

logging.getLogger("pyvpdriver").setLevel(logging.WARNING)

_LOGGER = logging.getLogger(__name__)


@dataclass
class RuntimeData:
    """Class to hold runtime integration data."""
    coordinator: DavisVantageDataUpdateCoordinator


type DavisConfigEntry = ConfigEntry[RuntimeData]


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: DavisConfigEntry
) -> bool:
    """Migrate legacy mixed data/options entries to version 2."""
    if config_entry.version >= 2:
        return True

    data = dict(config_entry.data)
    options = dict(config_entry.options)
    for key, default in (
        (CONFIG_INTERVAL, 30),
        (CONF_USE_LOOP2, False),
        (CONFIG_PERSISTENT_CONNECTION, False),
    ):
        if key in data:
            options.setdefault(key, data.pop(key))
        else:
            options.setdefault(key, default)

    protocol = data.get(CONFIG_PROTOCOL, "")
    endpoint = data.get(CONFIG_LINK, "")
    try:
        detected = await hass.async_add_executor_job(
            default_verification_result, protocol, endpoint
        )
        data[CONFIG_LINK] = detected.endpoint
        data[CONFIG_LOOP2_SUPPORTED] = bool(
            data.get(CONFIG_LOOP2_SUPPORTED, options.get(CONF_USE_LOOP2, False))
        )
        if detected.baud_rate is not None:
            data.setdefault(CONFIG_BAUD_RATE, detected.baud_rate)
        else:
            data.pop(CONFIG_BAUD_RATE, None)

        if detected.identity_strength == IDENTITY_STRONG:
            identity = detected.identity
            source = detected.identity_source
            strength = detected.identity_strength
        else:
            identity = data.get(CONFIG_IDENTITY, f"generated:{uuid4()}")
            source = data.get(CONFIG_IDENTITY_SOURCE, "generated")
            strength = data.get(
                CONFIG_IDENTITY_STRENGTH, detected.identity_strength
            )
    except Exception as err:
        _LOGGER.warning("Could not canonicalize legacy Davis entry: %s", err)
        identity = data.get(CONFIG_IDENTITY, f"generated:{uuid4()}")
        source = data.get(CONFIG_IDENTITY_SOURCE, "generated")
        strength = data.get(CONFIG_IDENTITY_STRENGTH, "weak")

    data[CONFIG_IDENTITY] = identity
    data[CONFIG_IDENTITY_SOURCE] = source
    data[CONFIG_IDENTITY_STRENGTH] = strength
    unique_id = config_entry.unique_id or f"davis:{identity}"
    hass.config_entries.async_update_entry(
        config_entry,
        data=data,
        options=options,
        unique_id=unique_id,
        version=2,
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant, config_entry: DavisConfigEntry
) -> bool:
    """Set up Davis Vantage from a config entry."""
    _LOGGER.debug("Setting up entry %s with data: %s", config_entry.entry_id, config_entry.data)
    _LOGGER.debug("Entry options: %s", config_entry.options)

    protocol = config_entry.data.get(CONFIG_PROTOCOL, "")
    link = config_entry.data.get(CONFIG_LINK, "")

    persistent_connection = config_entry.options.get(
        CONFIG_PERSISTENT_CONNECTION, False
    )

    baud_rate = config_entry.data.get(CONFIG_BAUD_RATE, DEFAULT_BAUD_RATE)
    use_loop2 = config_entry.options.get(CONF_USE_LOOP2, False) and bool(
        config_entry.data.get(CONFIG_LOOP2_SUPPORTED, False)
    )
    client = DavisVantageClient(
        hass,
        protocol,
        link,
        persistent_connection,
        use_loop2=use_loop2,
        baud_rate=baud_rate,
    )

    try:
        await client.connect_to_station()
        await client.get_station_info()
    except Exception as err:
        _LOGGER.warning(
            "Could not connect to Davis Vantage console at %s (%s). Will retry in background.",
            link,
            err,
        )
        await client.async_begin_shutdown()
        await client.async_close()
        raise ConfigEntryNotReady(f"Failed to connect to Davis station: {err}") from err

    device_info = DeviceInfo(
        identifiers={
            (
                DOMAIN,
                config_entry.data.get(CONFIG_IDENTITY, config_entry.entry_id),
            )
        },
        manufacturer=MANUFACTURER,
        name=NAME,
        model=config_entry.data.get(CONFIG_STATION_MODEL, "Davis Vantage Weather Station"),
        sw_version=getattr(client, "firmware_version", None),
        hw_version=None,
    )

    coordinator = DavisVantageDataUpdateCoordinator(
        hass=hass,
        client=client,
        device_info=device_info,
        config_entry=config_entry
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning("Initial refresh failed for Davis Vantage (%s). Retrying...", err)
        # Release the port now or HA's ConfigEntryNotReady retry hits "Resource busy".
        await client.async_begin_shutdown()
        await client.async_close()
        raise ConfigEntryNotReady(f"Initial data fetch failed: {err}") from err

    config_entry.runtime_data = RuntimeData(coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    config_entry.async_on_unload(
        config_entry.add_update_listener(async_reload_entry)
    )

    async_setup_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: DavisConfigEntry) -> bool:
    """Unload a config entry and release serial/network resources."""
    coordinator = config_entry.runtime_data.coordinator
    client = coordinator.client
    await client.async_begin_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )

    if not unload_ok:
        # The entry stays loaded, so undo the shutdown begun above.
        await client.async_cancel_shutdown()
        return False

    close_ok = await client.async_close()
    if not close_ok:
        _LOGGER.error("Davis transport did not confirm closure; unload refused")
        await client.async_cancel_shutdown()
        return False

    ir.async_delete_issue(hass, DOMAIN, "connection_lost")

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, config_entry: DavisConfigEntry) -> None:
    """Reload config entry when options are updated."""
    await hass.config_entries.async_reload(config_entry.entry_id)
