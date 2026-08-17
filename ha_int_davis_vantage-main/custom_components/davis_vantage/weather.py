"""Weather platform for Davis Vantage."""
from __future__ import annotations

from homeassistant.components.weather import (
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

class DavisWeatherEntity(CoordinatorEntity, WeatherEntity):
    """Representation of a Davis Vantage Weather Station."""

    # HA will automatically convert these if the user prefers Metric
    _attr_native_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_native_pressure_unit = UnitOfPressure.INHG
    _attr_native_wind_speed_unit = UnitOfSpeed.MILES_PER_HOUR
    _attr_native_precipitation_unit = UnitOfLength.INCHES
    # --- FIX: DEFINE SUPPORTED FEATURES FOR THE WEATHER CARD ---
    # Set to 0 if only current conditions are supported, 
    # or combine flags if forecast methods are implemented.
    _attr_supported_features = WeatherEntityFeature(0)
    # -----------------------------------------------------------
    
    # 1. Add entry_id: str to the parameters
    def __init__(self, coordinator, entry_id: str):
        super().__init__(coordinator)
        
        # 2. Swap mac_address for entry_id
        self._attr_unique_id = f"{entry_id}_weather"
        
        self._attr_name = "Vantage Weather Station"
        self._attr_device_info = coordinator.device_info
        
    @property
    def condition(self) -> str | None:
        """Map Davis Forecast Icon to HA conditions."""
        if not self.coordinator.data:
            return None
            
        try:
            raw_icon = self.coordinator.data.get('ForecastIcon') # Reverted to ForecastIcon
            if raw_icon is None or raw_icon == "" or raw_icon in (255, 32767, -32768):
                icon_val = -1
            else:
                icon_val = int(raw_icon)
        except (ValueError, TypeError):
            icon_val = -1

        # Correct Davis Protocol v2.61 Icon Mapping
        mapping = {
            0: "sunny",  # Default state when console is waiting for 3-hr barometric trend
            # Correct Davis Protocol v2.50/v2.61 Icon Mapping based on official spec sheet
            8: "sunny",          # Sun (Mostly Clear)
            6: "partlycloudy",   # Partial Sun + Cloud (Partly Cloudy)
            2: "cloudy",         # Cloud (Mostly Cloudy)
            7: "rainy",          # Partial Sun + Cloud + Rain (Rain within 12 hrs)
            3: "cloudy",          # Cloud + Rain (Rain within 12 hrs)
            18: "snowy",         # Cloud + Snow (Snow within 12 hrs)
            22: "snowy",         # Partial Sun + Cloud + Snow (Snow within 12 hrs)
            19: "snowy-rainy",   # Cloud + Rain + Snow (Rain or Snow within 12 hrs)
            23: "snowy-rainy",   # Partial Sun + Cloud + Rain + Snow (Rain or Snow)
        }
        
        condition_state = mapping.get(icon_val)
        
        if condition_state is None:
            try:
                rain_rate = self.coordinator.data.get('RainRate') # Reverted to RainRate
                
                if rain_rate is None or rain_rate in (255, 32767, -32768):
                    rain_rate = 0
                    
                if float(rain_rate) > 0:
                    condition_state = "rainy"
                else:
                    condition_state = "partlycloudy"
            except (ValueError, TypeError):
                condition_state = "partlycloudy"
        
        if condition_state == "sunny":
            try:
                from homeassistant.helpers.sun import is_up
                if not is_up(self.hass):
                    return "clear-night"
            except Exception:
                pass
                
        return condition_state

    @property
    def native_temperature(self) -> float | None:
        return self.coordinator.data.get("TempOut")
        return float(val) if val is not None else None

    @property
    def humidity(self) -> float | None:
        return self.coordinator.data.get("HumOut")
        return float(val) if val is not None else None

    @property
    def native_pressure(self) -> float | None:
        return self.coordinator.data.get("Barometer")
        return float(val) if val is not None else None

    @property
    def native_wind_speed(self) -> float | None:
        return self.coordinator.data.get("WindSpeed")
        return float(val) if val is not None else None

    @property
    def wind_gust_speed(self) -> float | None:
        """Return the wind gust speed."""
        # If the integration hasn't pulled any data yet during startup, stay None
        if not self.coordinator.data:
            return None
            
        # Grab the gust speed from the dictionary 
        # (Make sure the key matches exactly what you use, e.g., "WindSpeed10Min" or "WindGust")
        value = self.coordinator.data.get("WindSpeed10Min") 
        
        # Intercept the Davis "Missing Data" byte (255) or a missing key and return 0 mph
        if value is None or value == 255:
            return 0
            
        return value

    @property
    def wind_bearing(self) -> float | None:
        return self.coordinator.data.get("WindDir")

    @property
    def native_apparent_temperature(self) -> float | None:
        """Return the feels-like temperature in °F."""
        # Davis provides several options; HeatIndex or THSWIndex are most common for apparent temp
        return self.coordinator.data.get("HeatIndex") 

    @property
    def native_dew_point(self) -> float | None:
        """Return the dew point temperature in °F."""
        return self.coordinator.data.get("DewPoint")



async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Davis Vantage weather entity based on a config entry."""
    entry_data = hass.data["davis_vantage"][entry.entry_id]

    # Handle the coordinator extraction
    if isinstance(entry_data, dict) and "coordinator" in entry_data:
        coordinator = entry_data["coordinator"]
    elif hasattr(entry_data, "coordinator"):
        coordinator = entry_data.coordinator
    else:
        coordinator = entry_data

    # Ensure the class name below matches your actual class definition lower in the file
    # If your class is named DavisWeatherEntity, change it here.
    # 3. Pass entry.entry_id into the class
    async_add_entities([DavisWeatherEntity(coordinator, entry.entry_id)])

