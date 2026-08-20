from datetime import timedelta
from typing import Any
import logging
import inspect
import asyncio

from homeassistant import config_entries
from homeassistant.helpers.update_coordinator import UpdateFailed, DataUpdateCoordinator
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.core import HomeAssistant

from .client import DavisVantageClient
from .const import DOMAIN

_LOGGER: logging.Logger = logging.getLogger(__package__)


class DavisVantageDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the weather station."""

    def __init__(
        self, 
        hass: HomeAssistant, 
        client: DavisVantageClient, 
        device_info: DeviceInfo, 
        config_entry: config_entries.ConfigEntry
    ) -> None:
        """Initialize."""
        # Grab interval safely from options, falling back to data, defaulting to 300
        interval = config_entry.options.get(
            "interval", 
            config_entry.data.get("interval", 300)
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
            config_entry=config_entry,
        )
        
        self.client: DavisVantageClient = client
        self.platforms: list[str] = []
        self.last_updated = None
        self.device_info = device_info
        self.config_entry = config_entry

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via library with HA startup safeguards."""
        try:
            # Enforce a 15-second timeout to accommodate wake-up retries and parsing
            async with asyncio.timeout(15):
                
                # 1. Call the function directly WITHOUT the executor wrapper
                data = self.client.async_get_current_data()
                
                # 2. The Ultimate Coroutine Unwrapper
                while inspect.isawaitable(data):
                    data = await data
                    
                return data

        except TimeoutError as exception:
            _LOGGER.warning("Davis station is unresponsive - timing out to protect HA startup.")
            raise UpdateFailed("Timeout: Davis station took too long to respond") from exception
        except ValueError as exception:
            _LOGGER.error("Serial alignment failure: %s", exception)
            raise UpdateFailed(f"Serial protocol mismatch: {exception}") from exception
        except Exception as exception:
            _LOGGER.error("Error fetching Davis data: %s", exception)
            raise UpdateFailed(f"Error fetching Davis data: {exception}") from exception