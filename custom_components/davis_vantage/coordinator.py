from datetime import timedelta
from typing import Any
import logging
import inspect
import asyncio

from homeassistant import config_entries
from homeassistant.helpers.update_coordinator import UpdateFailed, DataUpdateCoordinator
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers import issue_registry as ir
from homeassistant.core import HomeAssistant

from .client import DavisVantageClient
from .const import DOMAIN

_LOGGER: logging.Logger = logging.getLogger(__package__)

# Serial hiccups are common on this hardware; one dropped poll must not raise an issue.
CONNECTION_ISSUE_THRESHOLD = 3
CONNECTION_ISSUE_ID = "connection_lost"


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
        # Runtime tuning has one owner: ConfigEntry.options.
        interval = config_entry.options.get("interval", 30)

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
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        """Expose the availability streak for diagnostics."""
        return self._consecutive_failures

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via library with HA startup safeguards."""
        try:
            # 15 s covers wake-up retries plus parsing.
            async with asyncio.timeout(15):

                data = self.client.async_get_current_data()

                while inspect.isawaitable(data):
                    data = await data

                # The client reports LastError instead of raising; without this an unreachable console counts as success.
                if data.get("LastError"):
                    raise UpdateFailed(data["LastError"])

                self._on_update_success()
                return data

        except TimeoutError as exception:
            _LOGGER.warning("Davis station is unresponsive - timing out to protect HA startup.")
            self._on_update_failure()
            raise UpdateFailed("Timeout: Davis station took too long to respond") from exception
        except UpdateFailed:
            self._on_update_failure()
            raise
        except ValueError as exception:
            _LOGGER.error("Serial alignment failure: %s", exception)
            self._on_update_failure()
            raise UpdateFailed(f"Serial protocol mismatch: {exception}") from exception
        except Exception as exception:
            _LOGGER.error("Error fetching Davis data: %s", exception)
            self._on_update_failure()
            raise UpdateFailed(f"Error fetching Davis data: {exception}") from exception

    def _on_update_success(self) -> None:
        """Reset the failure streak and clear any open connection-lost repair issue."""
        if self._consecutive_failures >= CONNECTION_ISSUE_THRESHOLD:
            ir.async_delete_issue(self.hass, DOMAIN, CONNECTION_ISSUE_ID)
        self._consecutive_failures = 0

    def _on_update_failure(self) -> None:
        """Track the failure streak and raise a repair issue once it's sustained."""
        self._consecutive_failures += 1
        if self._consecutive_failures == CONNECTION_ISSUE_THRESHOLD:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                CONNECTION_ISSUE_ID,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="connection_lost",
                translation_placeholders={
                    "name": self.config_entry.title if self.config_entry else "Davis Vantage"
                },
            )
