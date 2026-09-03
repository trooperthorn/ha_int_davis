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


_LOGGER = logging.getLogger(__name__)

# All entities read from the shared coordinator's already-polled data rather
# than doing their own I/O, so there's nothing for HA to serialize here.
PARALLEL_UPDATES = 0

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
        
    # Lowercase to match the state keys in translations/en.json and
    # icons.json (both already have a full 16-point table for
    # wind_direction_rose that never matched anything while this returned
    # uppercase strings).
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
    
    # Custom function to extract the specific datapoint from the Davis LOOP dictionary
    value_fn: Callable[[dict], float | int | str | None]


# ---------------------------------------------------------
# SENSOR DEFINITIONS
# Mapped directly to the Davis Vantage Serial Protocol v2.61
# ---------------------------------------------------------
SENSOR_TYPES: tuple[DavisSensorEntityDescription, ...] = (
    DavisSensorEntityDescription(
        # NOTE: this used to share key="ForecastIcon" with the "condition"
        # entry below, which gives both the same unique_id - HA silently
        # drops whichever loses that race, so this entity could never
        # actually be enabled. Gave it its own key.
        key="forecast_icon_condition",
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
        value_fn=lambda data: _map_forecast_icon(
            data,
            {
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
            {
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
        # Set to False to disable this sensor by default
        entity_registry_enabled_default=False,
        value_fn=lambda data: (
            str(val) if (val := safe_get(data, 'ForecastIcon')) is not None else "Missing"
        ),
    ),
    # --- TEMPERATURE & HUMIDITY ---
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
        value_fn=lambda data: data['TempIn'], # Use the exact PyVantagePro key here
 #       entity_category=EntityCategory.Inside, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        key="HumOut",
        translation_key="humidity",
        name="Outside Humidity",
        icon="mdi:water-percent",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['HumOut'], # Use the exact PyVantagePro key here
    ),
    DavisSensorEntityDescription(
        key="inside_humidity",
        translation_key="humidity_inside",
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
        translation_key="wind_speed",
        name="Wind Speed",
        icon="mdi:weather-windy",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['WindSpeed'], # Use the exact PyVantagePro key here
  #      entity_category=EntityCategory.Climate, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        # NOTE: "WindSpeed10Min" is the 10-minute *average* wind speed, not a
        # gust (Davis manual, LOOP data format). The key is kept as-is so
        # existing entity_ids/history aren't broken by the rename.
        key="wind_speed_10_min_gust",
        translation_key="wind_speed_average_10min",
        name="Wind Speed (10 min Avg)",
        icon="mdi:weather-windy",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['WindSpeed10Min'], # Use the exact PyVantagePro key here
    ),
    DavisSensorEntityDescription(
        key="wind_gust",
        translation_key="wind_gust",
        name="Wind Gust",
        icon="mdi:weather-windy-variant",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        # True gust: the high wind speed from the last archive interval
        # (client.py's add_archive_info), not the 10-min average.
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
        # Notice we don't use a unit_of_measurement or state_class
        # because this is a text string, not a numerical measurement!
        value_fn=lambda data: get_wind_rose(_wind_dir_or_none(data)),
    ),
    DavisSensorEntityDescription(
        # Only populated in LOOP2 mode (add_loop2_wind_info in client.py) -
        # the direction of the last 10-minute gust, not an average direction.
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

    # --- PRECIPITATION (RATES & CUMULATIVE) ---
    DavisSensorEntityDescription(
        key="rain_rate",
        translation_key="rain_rate",
        name="Rain Rate",
        icon="mdi:weather-pouring",
        device_class=SensorDeviceClass.PRECIPITATION_INTENSITY,
        native_unit_of_measurement=UnitOfVolumetricFlux.INCHES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT, # Rate fluctuates up and down
        value_fn=lambda data: data['RainRate'], # Use the exact PyVantagePro key here
 #       entity_category=EntityCategory.Rain, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        # Available in both LOOP1 and LOOP2. Referenced by
        # blueprints/automation/flash_flood.yaml, which previously pointed
        # at a sensor that didn't exist.
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
        # LOOP2-only. Same blueprint reference as rain_storm above.
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
        state_class=SensorStateClass.TOTAL_INCREASING, # MUST be TOTAL_INCREASING for HA Energy/Water dashboard
        value_fn=lambda data: data['RainDay'], # Use the exact PyVantagePro key here
#        entity_category=EntityCategory.Rain, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        key="rain_month",
        translation_key="rain_month",
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
        translation_key="rain_year",
        name="Rain Year",
        icon="mdi:water",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement=UnitOfLength.INCHES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data['RainYear'], # Use the exact PyVantagePro key here
 #       entity_category=EntityCategory.Rain, # Hides it from the main dashboard & Voice assistants
    ),
    DavisSensorEntityDescription(
        # Evapotranspiration - useful for irrigation-controller integrations
        # (e.g. rain-delay/watering-need logic) that expect this alongside rainfall.
        key="et_day",
        translation_key="et_day",
        name="Evapotranspiration Today",
        icon="mdi:sprout-outline",
        native_unit_of_measurement=UnitOfLength.INCHES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get('ETDay'),
    ),

    # --- SOLAR & UV ---
    DavisSensorEntityDescription(
        key="solar_radiation",
        translation_key="solar_radiation",
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
        translation_key="uv_level",
        name="UV Index",
        icon="mdi:weather-sunny-alert",
        # HA does not currently enforce a UV device class natively, so we just use MEASUREMENT
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data['UV'], # Use the exact PyVantagePro key here
  #      entity_category=EntityCategory.Sun, # Hides it from the main dashboard & Voice assistants
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
        # THSW (Temp-Humidity-Sun-Wind) index: Davis's own apparent-temperature
        # figure, only available in LOOP2 mode - see LoopData2Parser.
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

    # --- DIAGNOSTICS & SYSTEM STATUS ---
    DavisSensorEntityDescription(
        key="console_battery",
        translation_key="battery_voltage",
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

    # Tells Home Assistant to use the device name + translation_key/name,
    # matching the pattern already used in binary_sensor.py.
    _attr_has_entity_name = True
    entity_description: DavisSensorEntityDescription

    # --- UPDATE THIS INIT FUNCTION ---
    def __init__(self, coordinator, entry_id: str, description: DavisSensorEntityDescription) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        
        # Use the passed entry_id for the unique ID
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = coordinator.device_info
        # Tracks whether the last poll's missing-value warning already fired,
        # so a sensor that stays None for many consecutive polls (e.g. one
        # fed only by the best-effort archive fetch) logs once per outage
        # instead of once per poll.
        self._attr_missing_value_logged = False

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
                    # Previously stored values here are always float/int/str
                    # (see below) - narrow SensorEntity's broader StateType.
                    return cast("float | int | str", self._attr_native_value)
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
            # Using WARNING forces this to show in the standard log, but only
            # on the transition into "missing" - otherwise a sensor that
            # legitimately stays None for many consecutive polls (e.g. one
            # fed only by the best-effort archive fetch) would warn forever
            # with no way to suppress it via log-level configuration.
            if not self._attr_missing_value_logged:
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
                self._attr_missing_value_logged = True
        else:
            self._attr_missing_value_logged = False

        # Save the valid value to _attr_native_value so we can use it for retention later
        self._attr_native_value = value
        return value

