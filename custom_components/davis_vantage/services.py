"""Global services file."""

from typing import Any
from zoneinfo import ZoneInfo

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from pyvantagepro.utils import bytes_to_hex  # type: ignore

from .const import (
    DOMAIN,
    SERVICE_SET_DAVIS_TIME,
    SERVICE_GET_DAVIS_TIME,
    SERVICE_GET_RAW_DATA,
    SERVICE_SET_YEARLY_RAIN,
    SERVICE_SET_ARCHIVE_PERIOD,
    SERVICE_SET_RAIN_COLLECTOR,
    SERVICE_SET_BAROMETER_CALIBRATION,
    SERVICE_GET_EEPROM,
    SERVICE_SET_EEPROM,
    SERVICE_SET_CONSOLE_LAMPS,
    SERVICE_CLEAR_ALARMS,
    SERVICE_GET_INFO,
    RAIN_COLLECTOR_IMPERIAL,
    RAIN_COLLECTOR_METRIC,
    RAIN_COLLECTOR_METRIC_0_1,
)
from .coordinator import DataUpdateCoordinator
from .utils import convert_to_iso_datetime

SET_YEARLY_RAIN_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("rain_clicks"): int
    }
)

SET_ARCHIVE_PERIOD_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("archive_period"): vol.In(
            ["1", "5", "10", "15", "30", "60", "120"]
        )
    }
)

SET_RAIN_COLLECTOR_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("rain_collector"): vol.In(
            [
                RAIN_COLLECTOR_IMPERIAL,
                RAIN_COLLECTOR_METRIC,
                RAIN_COLLECTOR_METRIC_0_1,
            ]
        )
    }
)

SET_BAROMETER_CALIBRATION_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("elevation"): vol.All(int, vol.Range(min=-2000, max=15000)),
        vol.Optional("barometer", default=0.0): vol.All(
            vol.Coerce(float), vol.Any(0, vol.Range(min=20.0, max=32.5))
        ),
    }
)

HEX_ADDRESS = vol.Match(r"^[0-9A-Fa-f]{1,3}$")
HEX_BYTES = vol.Match(r"^([0-9A-Fa-f]{2})+$")

SET_CONSOLE_LAMPS_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("state"): bool,
    }
)

GET_EEPROM_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("address"): HEX_ADDRESS,
        vol.Required("size"): vol.All(int, vol.Range(min=1, max=256)),
    }
)

SET_EEPROM_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("address"): HEX_ADDRESS,
        vol.Required("data"): HEX_BYTES,
    }
)


class DavisServicesSetup:
    """Class to handle Integration Services."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialise services."""
        self.hass = hass
        self.config_entry = config_entry
        self.coordinator: DataUpdateCoordinator = config_entry.runtime_data.coordinator

        self.setup_services()

    def setup_services(self):
        """Initialise the services in Hass."""
        # Use the synchronous 'register' because this runs in a background thread
        self.hass.services.register(
            DOMAIN,
            SERVICE_SET_DAVIS_TIME,
            self.set_davis_time
        )

        self.hass.services.register(
            DOMAIN,
            SERVICE_GET_DAVIS_TIME,
            self.get_davis_time,
            supports_response=SupportsResponse.ONLY,
        )

        self.hass.services.register(
            DOMAIN,
            SERVICE_GET_RAW_DATA,
            self.get_raw_data,
            supports_response=SupportsResponse.ONLY,
        )

        self.hass.services.register(
            DOMAIN,
            SERVICE_GET_INFO,
            self.get_info,
            supports_response=SupportsResponse.ONLY
        )

        self.hass.services.register(
            DOMAIN,
            SERVICE_SET_YEARLY_RAIN,
            self.set_yearly_rain,
            schema=SET_YEARLY_RAIN_SERVICE_SCHEMA,
        )

        self.hass.services.register(
            DOMAIN,
            SERVICE_SET_ARCHIVE_PERIOD,
            self.set_archive_period,
            schema=SET_ARCHIVE_PERIOD_SERVICE_SCHEMA,
        )

        self.hass.services.register(
            DOMAIN,
            SERVICE_SET_RAIN_COLLECTOR,
            self.set_rain_collector,
            schema=SET_RAIN_COLLECTOR_SERVICE_SCHEMA,
        )

        self.hass.services.register(
            DOMAIN,
            SERVICE_SET_BAROMETER_CALIBRATION,
            self.set_barometer_calibration,
            schema=SET_BAROMETER_CALIBRATION_SERVICE_SCHEMA,
        )

        self.hass.services.register(
            DOMAIN,
            SERVICE_GET_EEPROM,
            self.get_eeprom,
            schema=GET_EEPROM_SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

        self.hass.services.register(
            DOMAIN,
            SERVICE_SET_EEPROM,
            self.set_eeprom,
            schema=SET_EEPROM_SERVICE_SCHEMA,
        )

        self.hass.services.register(
            DOMAIN,
            SERVICE_SET_CONSOLE_LAMPS,
            self.set_console_lamps,
            schema=SET_CONSOLE_LAMPS_SERVICE_SCHEMA,
        )

        self.hass.services.register(
            DOMAIN,
            SERVICE_CLEAR_ALARMS,
            self.clear_alarms,
        )

    async def set_davis_time(self, _: ServiceCall) -> None:
        """Set Davis Time service"""
        client = self.config_entry.runtime_data.coordinator.client
        await client.async_set_davis_time()

    async def get_davis_time(self, _: ServiceCall) -> dict[str, Any]:
        """Get Davis Time service"""
        client = self.config_entry.runtime_data.coordinator.client
        davis_time = await client.async_get_davis_time()
        if davis_time is not None:
            return {
                "davis_time": convert_to_iso_datetime(
                    davis_time, ZoneInfo(self.hass.config.time_zone)
                )
            }
        else:
            return {"error": "Couldn't get davis time, please try again later"}

    async def get_raw_data(self, _: ServiceCall) -> dict[str, Any]:
        """Get Raw Data service"""
        client = self.config_entry.runtime_data.coordinator.client
        raw_data = client.get_raw_data()
        raw_data.update(client.get_raw_hilows())
        data: dict[str, Any] = {}
        for key in raw_data:  # type: ignore
            value = raw_data[key]  # type: ignore
            if isinstance(value, bytes):
                data[key] = bytes_to_hex(value)
            else:
                data[key] = value
        return data

    async def get_info(self, _: ServiceCall) -> dict[str, Any]:
        """Get Info service"""
        client = self.config_entry.runtime_data.coordinator.client
        info = await client.async_get_info()
        if info is not None:
            return info
        else:
            return {
                "error": "Couldn't get firmware information from Davis weather station"
            }

    async def set_yearly_rain(self, call: ServiceCall) -> None:
        """Set Yearly Rain service"""
        client = self.config_entry.runtime_data.coordinator.client
        await client.async_set_yearly_rain(call.data["rain_clicks"])

    async def set_archive_period(self, call: ServiceCall) -> None:
        """Set Archive Period service"""
        client = self.config_entry.runtime_data.coordinator.client
        await client.async_set_archive_period(call.data["archive_period"])
        client.clear_cached_property("archive_period")

    async def set_rain_collector(self, call: ServiceCall) -> None:
        """Set Rain Collector service"""
        client = self.config_entry.runtime_data.coordinator.client
        await client.async_set_rain_collector(call.data["rain_collector"])

    async def set_barometer_calibration(self, call: ServiceCall) -> None:
        """Set Barometer Calibration service"""
        client = self.config_entry.runtime_data.coordinator.client
        await client.async_set_barometer_calibration(
            call.data["elevation"], call.data.get("barometer", 0.0)
        )

    async def get_eeprom(self, call: ServiceCall) -> dict[str, Any]:
        """Get EEPROM service (advanced/diagnostic use)"""
        client = self.config_entry.runtime_data.coordinator.client
        data = await client.async_get_eeprom(call.data["address"], call.data["size"])
        return {"data": data}

    async def set_eeprom(self, call: ServiceCall) -> None:
        """Set EEPROM service (advanced use - see the manual's EEPROM address
        table before writing; some locations are factory calibration values
        that should never be written)."""
        client = self.config_entry.runtime_data.coordinator.client
        await client.async_set_eeprom(call.data["address"], call.data["data"])

    async def set_console_lamps(self, call: ServiceCall) -> None:
        """Set Console Lamps service"""
        client = self.config_entry.runtime_data.coordinator.client
        await client.async_set_console_lamps(call.data["state"])

    async def clear_alarms(self, _: ServiceCall) -> None:
        """Clear Active Alarms service"""
        client = self.config_entry.runtime_data.coordinator.client
        await client.async_clear_alarms()
