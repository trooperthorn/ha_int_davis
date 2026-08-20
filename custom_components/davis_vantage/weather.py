"""Weather platform for Davis Vantage."""
from __future__ import annotations

from homeassistant.components.weather import (
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import (
    UnitOfLength,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.sun import is_up
from homeassistant.util import dt as dt_util

# The single weather entity reads from the shared coordinator's already-
# polled data rather than doing its own I/O, so there's nothing for HA to
# serialize here.
PARALLEL_UPDATES = 0

# Davis Protocol v2.61 forecast icon -> HA condition string.
# See client.py add_additional_info / manual section IX.1 "Forecast Icons".
FORECAST_ICON_TO_CONDITION = {
    0: "sunny",  # Default state when console is waiting for 3-hr barometric trend
    8: "sunny",          # Sun (Mostly Clear)
    6: "partlycloudy",   # Partial Sun + Cloud (Partly Cloudy)
    2: "cloudy",         # Cloud (Mostly Cloudy)
    7: "rainy",          # Partial Sun + Cloud + Rain (Rain within 12 hrs)
    3: "cloudy",         # Cloud + Rain (Rain within 12 hrs)
    18: "snowy",         # Cloud + Snow (Snow within 12 hrs)
    22: "snowy",         # Partial Sun + Cloud + Snow (Snow within 12 hrs)
    19: "snowy-rainy",   # Cloud + Rain + Snow (Rain or Snow within 12 hrs)
    23: "snowy-rainy",   # Partial Sun + Cloud + Rain + Snow (Rain or Snow)
}


class DavisWeatherEntity(CoordinatorEntity, WeatherEntity):
    """Representation of a Davis Vantage Weather Station."""

    # HA will automatically convert these if the user prefers Metric
    _attr_native_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_native_pressure_unit = UnitOfPressure.INHG
    _attr_native_wind_speed_unit = UnitOfSpeed.MILES_PER_HOUR
    _attr_native_precipitation_unit = UnitOfLength.INCHES
    # The console has no multi-day/multi-hour numeric forecast - only a
    # single "next ~12 hours" icon + a canned text rule (see ForecastIcon /
    # ForecastRuleNo, also exposed as their own sensors). TWICE_DAILY with a
    # single condition-only entry is the closest honest fit; there is no
    # predicted temperature/wind/etc. to report.
    _attr_supported_features = WeatherEntityFeature.FORECAST_TWICE_DAILY

    # 1. Add entry_id: str to the parameters
    def __init__(self, coordinator, entry_id: str):
        super().__init__(coordinator)

        # 2. Swap mac_address for entry_id
        self._attr_unique_id = f"{entry_id}_weather"

        self._attr_name = "Vantage Weather Station"
        self._attr_device_info = coordinator.device_info

    def _forecast_condition(self) -> str | None:
        """Map the console's ForecastIcon (+ rain rate fallback) to an HA condition."""
        if not self.coordinator.data:
            return None

        try:
            raw_icon = self.coordinator.data.get('ForecastIcon')
            if raw_icon is None or raw_icon == "" or raw_icon in (255, 32767, -32768):
                icon_val = -1
            else:
                icon_val = int(raw_icon)
        except (ValueError, TypeError):
            icon_val = -1

        condition_state = FORECAST_ICON_TO_CONDITION.get(icon_val)

        if condition_state is None:
            try:
                rain_rate = self.coordinator.data.get('RainRate')

                if rain_rate is None or rain_rate in (255, 32767, -32768):
                    rain_rate = 0

                if float(rain_rate) > 0:
                    condition_state = "rainy"
                else:
                    condition_state = "partlycloudy"
            except (ValueError, TypeError):
                condition_state = "partlycloudy"

        return condition_state

    @property
    def condition(self) -> str | None:
        """Map Davis Forecast Icon to HA conditions."""
        condition_state = self._forecast_condition()

        if condition_state == "sunny":
            try:
                if not is_up(self.hass):
                    return "clear-night"
            except Exception:
                pass

        return condition_state

    async def async_forecast_twice_daily(self) -> list[Forecast] | None:
        """Return the console's single "next ~12 hours" outlook.

        The console has no per-day/per-hour predicted temperature, wind, or
        precipitation - only a condition icon and a text rule (ForecastRule
        sensor). This intentionally returns condition only; anything else
        would be fabricated, not a real forecast.
        """
        condition_state = self._forecast_condition()
        if condition_state is None:
            return None

        return [
            Forecast(
                datetime=dt_util.utcnow().isoformat(),
                is_daytime=is_up(self.hass),
                condition=condition_state,
            )
        ]

    @property
    def native_temperature(self) -> float | None:
        return self.coordinator.data.get("TempOut")

    @property
    def humidity(self) -> float | None:
        return self.coordinator.data.get("HumOut")

    @property
    def native_pressure(self) -> float | None:
        # The console physically cannot log a barometer reading outside
        # 20.000-32.500 inHg; anything outside that range is a bad read.
        pressure = self.coordinator.data.get("Barometer")
        if pressure is None or not (20.0 <= float(pressure) <= 32.5):
            return None
        return pressure

    @property
    def native_wind_speed(self) -> float | None:
        return self.coordinator.data.get("WindSpeed")

    @property
    def native_wind_gust_speed(self) -> float | None:
        """Return the true wind gust speed (last archive interval's high)."""
        if not self.coordinator.data:
            return None

        # "WindGust" is the archive-derived high wind speed (add_archive_info
        # in client.py). "WindSpeed10Min" is the 10-minute *average* speed,
        # not a gust - don't conflate the two.
        value = self.coordinator.data.get("WindGust")
        if value is None or value == 255:
            return None

        return value

    @property
    def wind_bearing(self) -> float | None:
        return self.coordinator.data.get("WindDir")

    @property
    def native_apparent_temperature(self) -> float | None:
        """Return the feels-like temperature in °F."""
        # THSW (Temp-Humidity-Sun-Wind) is Davis's own apparent-temperature
        # figure and is only available in LOOP2 mode; fall back to HeatIndex
        # (available in both modes) when it isn't.
        data = self.coordinator.data
        thsw = data.get("THSWIndex")
        return thsw if thsw is not None else data.get("HeatIndex")

    @property
    def native_dew_point(self) -> float | None:
        """Return the dew point temperature in °F."""
        return self.coordinator.data.get("DewPoint")

    @property
    def uv_index(self) -> float | None:
        """Return the current UV index."""
        return self.coordinator.data.get("UV")



async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Davis Vantage weather entity based on a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([DavisWeatherEntity(coordinator, entry.entry_id)])

