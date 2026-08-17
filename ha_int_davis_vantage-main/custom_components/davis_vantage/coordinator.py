from datetime import timedelta
from typing import Any
import logging
import inspect
import asyncio

from homeassistant import config_entries
from homeassistant.helpers.update_coordinator import UpdateFailed, DataUpdateCoordinator
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.core import HomeAssistant
from pyvantagepro.parser import LoopDataParserRevB

from .client import DavisVantageClient
from .const import DOMAIN

_LOGGER: logging.Logger = logging.getLogger(__package__)

def patch_vantagepro_to_loop2(device, use_loop2: bool):
    """
    Overrides the default get_current_data method in pyvantagepro.
    Checks the user's config to decide which LOOP packet to request.
    """
    # Prevent double-patching if the coordinator reloads
    if getattr(device, "_is_loop2_patched", False):
        return

    original_get_current_data = device.get_current_data

    def custom_get_current_data():
        device.link.wakeup()
        
        # Determine the correct serial command based on config
        if use_loop2:
            device.link.write(b"LPS 2 1\n")
        else:
            device.link.write(b"LOOP 1\n")
            
        # --- CRITICAL FIX: Serial Buffer Alignment ---
        # The wake-up command leaves \n\r in the buffer. 
        # We must iterate and discard stale bytes until we find the 0x06 ACK.
        ack = b""
        for _ in range(10):
            ack = device.link.read(1)
            if ack == b"\x06":
                break
                
        if ack != b"\x06":
            raise ValueError(f"Did not receive ACK (0x06) from console. Buffer returned: {ack}")
            
        # The next 99 bytes are guaranteed to be the aligned data payload
        raw_data = device.link.read(99)
        
        # Parse based on expected packet type
        if use_loop2:
            return device.parser.parse_loop2(raw_data)
        else:
            return device.parser.parse(raw_data)

    device.get_current_data = custom_get_current_data
    device._is_loop2_patched = True


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

        # --- CRITICAL FIX: BRIDGE THE HARDWARE LINK ---
        if not hasattr(self.client, 'link'):
            for attr_name in ('vantage', 'vantagepro', 'device', '_device', 'vantage_pro', 'client', '_client', 'vp2'):
                hw_object = getattr(self.client, attr_name, None)
                if hw_object and hasattr(hw_object, 'link'):
                    self.client.link = hw_object.link
                    _LOGGER.debug(f"Successfully bridged hardware link via {attr_name}")
                    break
        # ----------------------------------------------

        # --- CRITICAL FIX: UNCOMMENTED CONFIG RETRIEVAL ---
        # Fetch user preference for Loop 2
        use_loop2 = config_entry.options.get(
            "use_loop2", 
            config_entry.data.get("use_loop2", False)
        )
        
        # Apply the patch if requested
        if use_loop2:
            patch_vantagepro_to_loop2(self.client, use_loop2)

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