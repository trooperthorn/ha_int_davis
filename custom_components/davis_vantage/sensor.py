"""Sensor platform for Davis Vantage."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfRatio,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfIrradiance,
    UnitOfLength,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfVolumetricFlux,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity


_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

def safe_get(data, key):
    """Safely extract a value from parser objects or dictionaries."""
    try:
        return data[key]
    except (KeyError, TypeError, AttributeError, IndexError):
        return None

def get_wind_rose(degrees: float | int | str | None) -> str | None:
    """Convert wind direction in degrees to a compass rose string."""
    # 255, 32767 and -32768 are Davis dash values, not bearings.
    if degrees is None or degrees in (255, 32767, 32768, -32768):
        return None
        
    try:
        deg = float(degrees)
    except (ValueError, TypeError):
        return None
        
    # Lowercase to match the wind_direction_rose state keys in translations/en.json.
    compass_points = [
        "n", "nne", "ne", "ene", "e", "ese", "se", "sse",
        "s", "ssw", "sw", "wsw", "w", "wnw", "nw", "nnw", "n"
    ]

    # Each sector is 22.5 degrees. Shift by 11.25 to center North on 0/360.
    idx = int((deg + 11.25) / 22.5) % 16
    return compass_points[idx]


def _float_or_none(data: dict, key: str) -> float | None:
    """Read `key` from `data` as a float, or None if missing."""
    value = data.get(key)
    return float(value) if value is not None else None


def _wind_dir_or_none(data: dict) -> float | None:
    """Read WindDir as a float, treating calm/dashed (0, 32767) as unknown."""
    value = data.get('WindDir')
    return float(value) if value not in (None, 0, 32767) else None


def _barometer_or_none(data: dict) -> float | None:
    """Read Barometer, discarding readings outside the console's 20.0-32.5 inHg range."""
    value = data.get('Barometer')
    if value is None:
        return None
    value = float(value)
    return value if 20.0 <= value <= 32.5 else None

def _map_forecast_icon(data: dict, mapping: dict[int, str], default: str) -> str:
    """Map the Davis ForecastIcon field through `mapping`, falling back to `default`."""
    raw_icon = data.get("ForecastIcon")
    try:
        icon_val = (
            int(raw_icon) if raw_icon not in (None, "", 255, 32767, -32768) else -1
        )
    except (TypeError, ValueError):
        icon_val = -1
    return mapping.get(icon_val, default)


@dataclass(frozen=True, kw_only=True)
class DavisSensorEntityDescription(SensorEntityDescription):
    """Class describing Davis Vantage sensor entities."""
    
    value_fn: Callable[[dict], float | int | str | None]


SENSOR_TYPES: tuple[DavisSensorEntityDescription, ...] = (
    DavisSensorEntityDescription(
        key="forecast_icon_condition",
        translation_key="forecast_icon",
        name="Current Condition",
        device_class=SensorDeviceClass.ENUM,
        icon="mdi:weather-partly-cloudy",
        entity_registry_enabled_default=False,
        options=[
            "sunny",
            "partlycloudy",
            "cloudy",
            "rainy",
            "snowy",
            "snowy-rainy",
        ],
        value_fn=lambda data: _map_forecast_icon(
            data,
            # Icon codes: see FORECAST_ICON_TO_CONDITION in weather.py.
            {
                0: "sunny",
                8: "sunny",
                6: "partlycloudy",
                2: "cloudy",
                7: "rainy",
                3: "cloudy",
                18: "snowy",
                22: "snowy",
                19: "snowy-rainy",
                23: "snowy-rainy",
            },
            "sunny",
        ),
    ),
    DavisSensorEntityDescription(
        key="ForecastIcon",
        translation_key="condition",
        name="Current Condition",
        device_class=SensorDeviceClass.ENUM,
        icon="mdi:weather-partly-cloudy",
        options=[
            "Sunny",
            "Partly Cloudy",
            "Cloudy",
            "Rainy",
            "Snowy",
            "Snowy Rainy",
        ],
        value_fn=lambda data: _map_forecast_icon(
            data,
            # Icon codes: see FORECAST_ICON_TO_CONDITION in weather.py.
            {
                0: "Sunny",
                8: "Sunny",
                6: "Partly Cloudy",
                2: "Cloudy",
                7: "Rainy",
                3: "Cloudy",
                18: "Snowy",
                22: "Snowy",
                19: "Snowy Rainy",
                23: "Snowy Rainy",
            },
            "Sunny",
        ),
    ),
    DavisSensorEntityDescription(
        key="forecast_rule",
        name="Forecast Rule",
        icon="mdi:script-text-outline",
        translation_key="forecast_rule",
        value_fn=lambda data: (
            str(val) if (val := safe_get(data, 'ForecastRuleNo')) is not None else None
        ),
    ),
    DavisSensorEntityDescription(
        key="forecast_icon_raw",
        translation_key="forecast_icon_raw",
        name="Forecast Icon Raw",
        icon="mdi:eye-check-outline",
        entity_registry_enabled_default=False,
        value_fn=lambda data: (
            str(val) if (val := safe_get(data, 'ForecastIcon')) is not None else "Missing"
        ),
    ),
    DavisSensorEntityDescription(
        key="TempOut",
        translation_key="temperature",
        name="Outside Temperature",
        icon="mdi:thermometer",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _float_or_none(data, 'TempOut'),
    ),
    DavisSensorEntityDescription(
        key="inside_temperature",
        translation_key="temperature_inside",
        name="Inside Temperature",
        icon="mdi:thermometer",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['TempIn'],
    ),
    DavisSensorEntityDescription(
        key="HumOut",
        translation_key="humidity",
        name="Outside Humidity",
        icon="mdi:water-percent",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['HumOut'],
    ),
    DavisSensorEntityDescription(
        key="inside_humidity",
        translation_key="humidity_inside",
        name="Inside Humidity",
        icon="mdi:water-percent",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=UnitOfRatio.PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['HumIn'],
    ),

    DavisSensorEntityDescription(
        key="WindSpeed",
        translation_key="wind_speed",
        name="Wind Speed",
        icon="mdi:weather-windy",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['WindSpeed'],
    ),
    DavisSensorEntityDescription(
        # WindSpeed10Min is a 10-minute average, not a gust; key kept so entity_ids survive.
        key="wind_speed_10_min_gust",
        translation_key="wind_speed_average_10min",
        name="Wind Speed (10 min Avg)",
        icon="mdi:weather-windy",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['WindSpeed10Min'],
    ),
    DavisSensorEntityDescription(
        key="wind_gust",
        translation_key="wind_gust",
        name="Wind Gust",
        icon="mdi:weather-windy-variant",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        # WindGust is the last archive interval's high, not the 10-min average.
        value_fn=lambda data: data.get('WindGust'),
    ),
   DavisSensorEntityDescription(
        key="wind_direction",
        translation_key="wind_direction",
        name="Wind Direction",
        icon="mdi:compass-outline",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_wind_dir_or_none,
    ),
    DavisSensorEntityDescription(
        key="wind_direction_rose",
        translation_key="wind_direction_rose",
        name="Wind Direction (Rose)",
        icon="mdi:compass",
        value_fn=lambda data: get_wind_rose(_wind_dir_or_none(data)),
    ),
    DavisSensorEntityDescription(
        # LOOP2-only: direction of the 10-minute gust, not an average direction.
        key="wind_gust_direction",
        translation_key="wind_gust_direction",
        name="Wind Gust Direction",
        icon="mdi:compass-outline",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get('WindGustDir'),
    ),
    DavisSensorEntityDescription(
        key="barometer",
        translation_key="barometric_pressure",
        name="Barometric Pressure",
        icon="mdi:gauge",
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        native_unit_of_measurement=UnitOfPressure.INHG,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_barometer_or_none,
    ),

    DavisSensorEntityDescription(
        key="rain_rate",
        translation_key="rain_rate",
        name="Rain Rate",
        icon="mdi:weather-pouring",
        device_class=SensorDeviceClass.PRECIPITATION_INTENSITY,
        native_unit_of_measurement=UnitOfVolumetricFlux.INCHES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['RainRate'],
    ),
    DavisSensorEntityDescription(
        key="rain_storm",
        translation_key="rain_storm",
        name="Rain Storm",
        icon="mdi:weather-pouring",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfLength.INCHES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get('RainStorm'),
    ),
    DavisSensorEntityDescription(
        # LOOP2-only.
        key="rain_15_min",
        translation_key="rain_15_min",
        name="Rain (15 Min)",
        icon="mdi:weather-pouring",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfLength.INCHES,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get('RainLast15Min'),
    ),
    DavisSensorEntityDescription(
        key="rain_day",
        translation_key="rain_day",
        name="Rain Today",
        icon="mdi:water",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfLength.INCHES,
        state_class=SensorStateClass.TOTAL_INCREASING,  # required by the HA Energy/Water dashboard
        value_fn=lambda data: data['RainDay'],
    ),
    DavisSensorEntityDescription(
        key="rain_month",
        translation_key="rain_month",
        name="Rain Month",
        icon="mdi:water",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfLength.INCHES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data['RainMonth'],
    ),
    DavisSensorEntityDescription(
        key="rain_year",
        translation_key="rain_year",
        name="Rain Year",
        icon="mdi:water",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfLength.INCHES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data['RainYear'],
    ),
    DavisSensorEntityDescription(
        key="et_day",
        translation_key="et_day",
        name="Evapotranspiration Today",
        icon="mdi:sprout-outline",
        native_unit_of_measurement=UnitOfLength.INCHES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get('ETDay'),
    ),

    DavisSensorEntityDescription(
        key="solar_radiation",
        translation_key="solar_radiation",
        name="Solar Radiation",
        icon="mdi:solar-power",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['SolarRad'],
    ),
    DavisSensorEntityDescription(
        key="uv_index",
        translation_key="uv_level",
        name="UV Index",
        icon="mdi:weather-sunny-alert",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['UV'],
    ),
    DavisSensorEntityDescription(
        key="HeatIndex",
        translation_key="heat_index",
        name="Heat Index",
        icon="mdi:thermometer",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _float_or_none(data, 'HeatIndex'),
    ),
    DavisSensorEntityDescription(
        key="WindChill",
        translation_key="wind_chill",
        name="Wind Chill",
        icon="mdi:snowflake-thermometer",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _float_or_none(data, 'WindChill'),
    ),
    DavisSensorEntityDescription(
        key="FeelsLike",
        translation_key="feels_like",
        name="Feels Like",
        icon="mdi:download-circle-outline",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _float_or_none(data, 'FeelsLike'),
    ),
    DavisSensorEntityDescription(
        # LOOP2-only.
        key="THSWIndex",
        translation_key="thsw_index",
        name="THSW Index",
        icon="mdi:sun-thermometer-outline",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get('THSWIndex'),
    ),
    DavisSensorEntityDescription(
        key="DewPoint",
        translation_key="dew_point",
        name="Dew Point",
        icon="mdi:water-thermometer-outline",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _float_or_none(data, 'DewPoint'),
    ),
    DavisSensorEntityDescription(
        key="BarTrend",
        translation_key="barometric_trend",
        name="Barometric Trend",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "falling_rapidly",
            "falling_slowly",
            "steady",
            "rising_slowly",
            "rising_rapidly"
        ],
        value_fn=lambda data: data.get("BarTrend"),
    ),

    DavisSensorEntityDescription(
        key="console_battery",
        translation_key="battery_voltage",
        name="Console Battery",
        icon="mdi:battery",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get('ConsoleBatteryVoltage')
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, 
    entry: ConfigEntry, 
    async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator

    entities = [
        DavisVantageSensor(
            coordinator=coordinator,
            entry_id=entry.entry_id,
            description=description
        )
        for description in SENSOR_TYPES
    ]
    
    async_add_entities(entities)


class DavisVantageSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Davis Vantage Sensor."""

    _attr_has_entity_name = True
    entity_description: DavisSensorEntityDescription

    def __init__(self, coordinator, entry_id: str, description: DavisSensorEntityDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = coordinator.device_info
        # Latches so a persistently missing value warns once per outage, not per poll.
        self._attr_missing_value_logged = False

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Disable optional sensors by default if they return no data on startup."""
        optional_keys = ("solar_radiation", "uv_index", "SolarRad", "UV")
        
        if self.entity_description.key in optional_keys:
            if not self.coordinator.data:
                return False

            try:
                val = self.entity_description.value_fn(self.coordinator.data)
            except (KeyError, TypeError):
                val = None
                
            # 255 is the Davis dash value for a sensor that is not fitted.
            if val is None or val == 255:
                _LOGGER.debug(
                    "Disabling optional sensor '%s' because no initial data was found.", 
                    self.entity_description.key
                )
                return False

        return getattr(self.entity_description, "entity_registry_enabled_default", True)

    @property
    def native_value(self) -> float | int | str | None:
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return None
            
        try:
            value = self.entity_description.value_fn(self.coordinator.data)
        except (KeyError, TypeError, AttributeError):
            value = None

        # 255, 32767 and -32768 are Davis dash values (lost sync or unplugged sensor).
        is_dash_value = value is None or value in (255, 32767, 32768, -32768)

        # Davis reports 0 or 32767 for calm air or lost sync; hold the last real direction.
        if self.entity_description.key in ("wind_direction", "WindDir", "wind_direction_rose", "WindRose"):
            if is_dash_value or value == 0 or value == "N":
                if getattr(self, "_attr_native_value", None) is not None:
                    return cast("float | int | str", self._attr_native_value)
                value = None

        elif self.entity_description.key in ("rain_rate", "RainRate") and is_dash_value:
            value = 0

        elif is_dash_value:
            value = None

        ignored_null_keys = (
            "solar_radiation", "uv_index", "SolarRad", "UV", 
            "wind_direction", "WindDir", "wind_direction_rose", "WindRose",
            "rain_rate", "RainRate"
        )
        
        if value is None and self.entity_description.key not in ignored_null_keys:
            # Warn only on the transition into missing; see docs/design.md.
            if not self._attr_missing_value_logged:
                _LOGGER.warning(
                    "Davis Sensor Alert: '%s' (key: %s) returned None.",
                    getattr(self.entity_description, "name", self.entity_description.key),
                    self.entity_description.key,
                )

                if self.entity_description.key in ("outside_temperature", "TempOut"):
                    try:
                        _LOGGER.warning(
                            "PyVantagePro Keys: %s", list(self.coordinator.data.keys())
                        )
                    except Exception:
                        _LOGGER.warning(
                            "PyVantagePro Properties: %s", dir(self.coordinator.data)
                        )
                self._attr_missing_value_logged = True
        else:
            self._attr_missing_value_logged = False

        self._attr_native_value = value
        return value

