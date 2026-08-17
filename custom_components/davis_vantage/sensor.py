"""Sensor platform for Davis Vantage."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
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

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

def safe_get(data, key):
    """Safely extract a value from parser objects or dictionaries."""
    try:
        return data[key]
    except (KeyError, TypeError, AttributeError, IndexError):
        return None

def get_wind_rose(degrees: float | int | str | None) -> str | None:
    """Convert wind direction in degrees to a compass rose string."""
    # Intercept Davis missing data flags so 255 doesn't get calculated as "WSW"
    if degrees is None or degrees in (255, 32767, 32768, -32768):
        return None
        
    try:
        deg = float(degrees)
    except (ValueError, TypeError):
        return None
        
    compass_points = [
        "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW", "N"
    ]
    
    # Each sector is 22.5 degrees. Shift by 11.25 to center North on 0/360.
    idx = int((deg + 11.25) / 22.5) % 16
    return compass_points[idx]

@dataclass(frozen=True, kw_only=True)
class DavisSensorEntityDescription(SensorEntityDescription):
    """Class describing Davis Vantage sensor entities."""
    
    # Custom function to extract the specific datapoint from the Davis LOOP dictionary
    value_fn: Callable[[dict], float | int | str | None]


# ---------------------------------------------------------
# SENSOR DEFINITIONS
# Mapped directly to the Davis Vantage Serial Protocol v2.61
# ---------------------------------------------------------
SENSOR_TYPES: tuple[DavisSensorEntityDescription, ...] = (
    DavisSensorEntityDescription(
        key="ForecastIcon",
        translation_key="forecast_icon",
        name="Current Condition",
        device_class=SensorDeviceClass.ENUM,
        icon="mdi:weather-partly-cloudy",
        # Set to False to disable this sensor by default
        entity_registry_enabled_default=False,
        options=[
            "sunny",
            "partlycloudy",
            "cloudy",
            "rainy",
            "snowy",
            "snowy-rainy",
        ],
        value_fn=lambda data: {
            0: "sunny",        # Default state when console is waiting for 3-hr barometric trend
            8: "sunny",        # Sun (Mostly Clear)
            6: "partlycloudy", # Partial Sun + Cloud (Partly Cloudy)
            2: "cloudy",       # Cloud (Mostly Cloudy)
            7: "rainy",        # Partial Sun + Cloud + Rain (Rain within 12 hrs)
            3: "cloudy",       # Cloud + Rain (Rain within 12 hrs)
            18: "snowy",       # Cloud + Snow (Snow within 12 hrs)
            22: "snowy",       # Partial Sun + Cloud + Snow (Snow within 12 hrs)
            19: "snowy-rainy", # Cloud + Rain + Snow (Rain or Snow within 12 hrs)
            23: "snowy-rainy", # Partial Sun + Cloud + Rain + Snow (Rain or Snow)
        }.get(data.get("ForecastIcon"), "sunny"),
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
        value_fn=lambda data: {
            0: "Sunny",        # Default state when console is waiting for 3-hr barometric trend
            8: "Sunny",        # Sun (Mostly Clear)
            6: "Partly Cloudy", # Partial Sun + Cloud (Partly Cloudy)
            2: "Cloudy",       # Cloud (Mostly Cloudy)
            7: "Rainy",        # Partial Sun + Cloud + Rain (Rain within 12 hrs)
            3: "Cloudy",       # Cloud + Rain (Rain within 12 hrs)
            18: "Snowy",       # Cloud + Snow (Snow within 12 hrs)
            22: "Snowy",       # Partial Sun + Cloud + Snow (Snow within 12 hrs)
            19: "Snowy Rainy", # Cloud + Rain + Snow (Rain or Snow within 12 hrs)
            23: "Snowy Rainy", # Partial Sun + Cloud + Rain + Snow (Rain or Snow)
        }.get(data.get("ForecastIcon"), "Sunny"),
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
        name="Forecast Icon Raw",
        icon="mdi:eye-check-outline",
        # Set to False to disable this sensor by default
        entity_registry_enabled_default=False,
        value_fn=lambda data: (
            str(val) if (val := safe_get(data, 'ForecastIcon')) is not None else "Missing"
        ),
    ),
    # --- TEMPERATURE & HUMIDITY ---
    DavisSensorEntityDescription(
        key="TempOut",
        name="Outside Temperature",
        icon="mdi:thermometer",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: float(data.get('TempOut')) if data.get('TempOut') is not None else None,
    ),
    DavisSensorEntityDescription(
        key="inside_temperature",
        name="Inside Temperature",
        icon="mdi:thermometer",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['TempIn'], # Use the exact PyVantagePro key here
 #       entity_category=EntityCategory.Inside, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        key="HumOut",
        name="Outside Humidity",
        icon="mdi:water-percent",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['HumOut'], # Use the exact PyVantagePro key here
    ),
    DavisSensorEntityDescription(
        key="inside_humidity",
        name="Inside Humidity",
        icon="mdi:water-percent",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['HumIn'], # Use the exact PyVantagePro key here
   #     entity_category=EntityCategory.Inside, # Hides it from the main dashboard & Voice assistants
    ),

    # --- WIND & PRESSURE ---
    DavisSensorEntityDescription(
        key="WindSpeed",
        name="Wind Speed",
        icon="mdi:weather-windy",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['WindSpeed'], # Use the exact PyVantagePro key here
  #      entity_category=EntityCategory.Climate, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        key="wind_speed_10_min_gust",
        name="Wind Gust (10 min)",
        icon="mdi:weather-windy",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['WindSpeed10Min'], # Use the exact PyVantagePro key here
    ),
   DavisSensorEntityDescription(
        key="wind_direction",
        name="Wind Direction",
        icon="mdi:compass-outline",
        native_unit_of_measurement="°",
        state_class=SensorStateClass.MEASUREMENT,
        # value_fn=lambda data: float(data.get('WindDir')) if data.get('WindDir') is not None else 0,
        # Updated Wind Direction to overcome manual data returned
        value_fn=lambda data: float(data.get('WindDir')) if data.get('WindDir') not in (None, 0, 32767) else None,
    ),
    DavisSensorEntityDescription(
        key="wind_direction_rose",
        name="Wind Direction (Rose)",
        icon="mdi:compass",
        # Notice we don't use a unit_of_measurement or state_class 
        # because this is a text string, not a numerical measurement!
        # value_fn=lambda data: get_wind_rose(data.get('WindDir')),
        # Updated Wind Rose
        value_fn=lambda data: get_wind_rose(data.get('WindDir')) if data.get('WindDir') not in (None, 0, 32767) else None,
    ),
    DavisSensorEntityDescription(
        key="barometer",
        name="Barometric Pressure",
        icon="mdi:gauge",
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        native_unit_of_measurement=UnitOfPressure.INHG,
        state_class=SensorStateClass.MEASUREMENT,
        # value_fn=lambda data: data['Barometer'], # Use the exact PyVantagePro key here
        # Updated Barometer Davis console physically cannot log barometer readings outside the 20.000 to 32.50 inHg range.
        value_fn=lambda data: data.get('Barometer') if data.get('Barometer') is not None and 20.0 <= float(data.get('Barometer')) <= 32.5 else None,
    ),

    # --- PRECIPITATION (RATES & CUMULATIVE) ---
    DavisSensorEntityDescription(
        key="rain_rate",
        name="Rain Rate",
        icon="mdi:weather-pouring",
        device_class=SensorDeviceClass.PRECIPITATION_INTENSITY,
        native_unit_of_measurement=UnitOfVolumetricFlux.INCHES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT, # Rate fluctuates up and down
        value_fn=lambda data: data['RainRate'], # Use the exact PyVantagePro key here
 #       entity_category=EntityCategory.Rain, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        key="rain_day",
        name="Rain Today",
        icon="mdi:water",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfLength.INCHES,
        state_class=SensorStateClass.TOTAL_INCREASING, # MUST be TOTAL_INCREASING for HA Energy/Water dashboard
        value_fn=lambda data: data['RainDay'], # Use the exact PyVantagePro key here
#        entity_category=EntityCategory.Rain, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        key="rain_month",
        name="Rain Month",
        icon="mdi:water",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfLength.INCHES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data['RainMonth'], # Use the exact PyVantagePro key here
#        entity_category=EntityCategory.Rain, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        key="rain_year",
        name="Rain Year",
        icon="mdi:water",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfLength.INCHES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data['RainYear'], # Use the exact PyVantagePro key here
 #       entity_category=EntityCategory.Rain, # Hides it from the main dashboard & Voice assistants
    ),

    # --- SOLAR & UV ---
    DavisSensorEntityDescription(
        key="solar_radiation",
        name="Solar Radiation",
        icon="mdi:solar-power",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['SolarRad'], # Use the exact PyVantagePro key here
  #      entity_category=EntityCategory.Sun, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        key="uv_index",
        name="UV Index",
        icon="mdi:weather-sunny-alert",
        # HA does not currently enforce a UV device class natively, so we just use MEASUREMENT
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['UV'], # Use the exact PyVantagePro key here
  #      entity_category=EntityCategory.Sun, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        key="HeatIndex",
        name="Heat Index",
        icon="mdi:thermometer",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: float(data.get('HeatIndex')) if data.get('HeatIndex') is not None else None,
    ),
    DavisSensorEntityDescription(
        key="WindChill",
        name="Wind Chill",
        icon="mdi:snowflake-thermometer",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: float(data.get('WindChill')) if data.get('WindChill') is not None else None,
    ),
    DavisSensorEntityDescription(
        key="FeelsLike",
        name="Feels Like",
        icon="mdi:download-circle-outline",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: float(data.get('FeelsLike')) if data.get('FeelsLike') is not None else None,
    ),
    DavisSensorEntityDescription(
        key="DewPoint",
        name="Dew Point",
        icon="mdi:water-thermometer-outline",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: float(data.get('DewPoint')) if data.get('DewPoint') is not None else None,
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

    # --- DIAGNOSTICS & SYSTEM STATUS ---
    DavisSensorEntityDescription(
        key="console_battery",
        name="Console Battery",
        icon="mdi:battery",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, # Hides it from the main dashboard & Voice assistants
        value_fn=lambda data: data.get('ConsoleBatteryVoltage') # Use the exact PyVantagePro key here
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, 
    entry: ConfigEntry, 
    async_add_entities: AddEntitiesCallback
) -> None:
    # Grab the coordinator safely from runtime_data
    coordinator = entry.runtime_data.coordinator # Ensure you access .coordinator here

    entities = [
        DavisVantageSensor(
            coordinator=coordinator,
            entry_id=entry.entry_id, # Pass entry_id explicitly
            description=description
        )
        for description in SENSOR_TYPES
    ]
    
    async_add_entities(entities)


class DavisVantageSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Davis Vantage Sensor."""

    entity_description: DavisSensorEntityDescription

    # --- UPDATE THIS INIT FUNCTION ---
    def __init__(self, coordinator, entry_id: str, description: DavisSensorEntityDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        
        # Use the passed entry_id for the unique ID
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = coordinator.device_info

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Disable optional sensors by default if they return no data on startup."""
        optional_keys = ("solar_radiation", "uv_index", "SolarRad", "UV")
        
        # Check if this specific sensor is one of the optional ones
        if self.entity_description.key in optional_keys:
            if not self.coordinator.data:
                return False
                
            # Safely extract the initial value during setup
            try:
                val = self.entity_description.value_fn(self.coordinator.data)
            except (KeyError, TypeError):
                val = None
                
            # If the initial value is None or the Davis "missing" value (255)
            if val is None or val == 255:
                _LOGGER.debug(
                    "Disabling optional sensor '%s' because no initial data was found.", 
                    self.entity_description.key
                )
                return False
                
        # Default to True (enabled) for all other sensors
        return getattr(self.entity_description, "entity_registry_enabled_default", True)

    @property
    def native_value(self) -> float | int | str | None:
        """Return the state of the sensor."""
        if not self.coordinator.data:
            return None
            
        # Use our custom value_fn from the Dataclass to grab the exact byte/value
        try:
            value = self.entity_description.value_fn(self.coordinator.data)
        except (KeyError, TypeError, AttributeError):
            value = None
            
        # 1. Identify if the value is a Davis dash/invalid value
        # 255, 32767, and -32768 indicate lost sync or unplugged sensors
        is_dash_value = value is None or value in (255, 32767, 32768, -32768)

        # 2. WIND DIRECTION RETENTION LOGIC
        # Davis uses 0 or 32767 to indicate calm air or lost sync
        if self.entity_description.key in ("wind_direction", "WindDir", "wind_direction_rose", "WindRose"):
            if is_dash_value or value == 0 or value == "N":
                # If the wind is calm, check if we have a saved previous direction
                if getattr(self, "_attr_native_value", None) is not None:
                    return self._attr_native_value
                # If fresh boot and no history, return None (Unknown) to keep graphs clean
                value = None

        # 3. RAIN RATE FALLBACK
        elif self.entity_description.key in ("rain_rate", "RainRate") and is_dash_value:
            # Force Rain Rate to 0 instead of Unknown/None when missing
            value = 0

        # 4. ALL OTHER SENSORS
        elif is_dash_value:
            value = None
            
        # --- LOG MISSING VALUES (runs even when debug logging is disabled) ---
        # Ignore expected-missing sensors
        ignored_null_keys = (
            "solar_radiation", "uv_index", "SolarRad", "UV", 
            "wind_direction", "WindDir", "wind_direction_rose", "WindRose",
            "rain_rate", "RainRate"
        )
        
        if value is None and self.entity_description.key not in ignored_null_keys:
            # Using WARNING forces this to show in the standard log
            _LOGGER.warning(
                "Davis Sensor Alert: '%s' (key: %s) returned None.",
                getattr(self.entity_description, "name", self.entity_description.key),
                self.entity_description.key,
            )
            
            # Detailed property dump specifically when outdoor temperature is missing
            if self.entity_description.key in ("outside_temperature", "TempOut"):
                try:
                    _LOGGER.warning(
                        "PyVantagePro Keys: %s", list(self.coordinator.data.keys())
                    )
                except Exception:
                    _LOGGER.warning(
                        "PyVantagePro Properties: %s", dir(self.coordinator.data)
                    )
                    
        # Save the valid value to _attr_native_value so we can use it for retention later
        self._attr_native_value = value
        return value
