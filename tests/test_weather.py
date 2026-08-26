"""Tests for the DavisWeatherEntity - forecast condition mapping, apparent
temperature fallback, pressure range filtering, and wind gust handling.

These instantiate the entity directly against a minimal fake coordinator
rather than going through async_setup_entry/config entries, since none of
this logic touches I/O or entity registration - just data massaging over
whatever the coordinator last polled.
"""
from unittest.mock import MagicMock, patch

import pytest

from custom_components.davis_vantage.weather import DavisWeatherEntity


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.device_info = MagicMock()


def make_entity(hass, data):
    entity = DavisWeatherEntity(_FakeCoordinator(data), "entry123")
    entity.hass = hass
    return entity


class TestForecastCondition:
    def test_known_icon_maps_to_condition(self, hass):
        entity = make_entity(hass, {"ForecastIcon": 6})
        assert entity._forecast_condition() == "partlycloudy"

    def test_unknown_icon_falls_back_to_rain_rate_when_raining(self, hass):
        entity = make_entity(hass, {"ForecastIcon": 999, "RainRate": 0.1})
        assert entity._forecast_condition() == "rainy"

    def test_unknown_icon_no_rain_falls_back_to_partlycloudy(self, hass):
        entity = make_entity(hass, {"ForecastIcon": 999, "RainRate": 0})
        assert entity._forecast_condition() == "partlycloudy"

    def test_missing_coordinator_data_returns_none(self, hass):
        entity = make_entity(hass, {})
        entity.coordinator.data = None
        assert entity._forecast_condition() is None

    @pytest.mark.parametrize("sentinel", [255, 32767, -32768])
    def test_dash_sentinel_icon_falls_back_to_rain_rate(self, hass, sentinel):
        entity = make_entity(hass, {"ForecastIcon": sentinel, "RainRate": 0})
        assert entity._forecast_condition() == "partlycloudy"


class TestCondition:
    def test_sunny_during_day_stays_sunny(self, hass):
        entity = make_entity(hass, {"ForecastIcon": 8})
        with patch("custom_components.davis_vantage.weather.is_up", return_value=True):
            assert entity.condition == "sunny"

    def test_sunny_at_night_becomes_clear_night(self, hass):
        entity = make_entity(hass, {"ForecastIcon": 8})
        with patch("custom_components.davis_vantage.weather.is_up", return_value=False):
            assert entity.condition == "clear-night"

    def test_non_sunny_condition_unaffected_by_day_night(self, hass):
        entity = make_entity(hass, {"ForecastIcon": 2})
        with patch("custom_components.davis_vantage.weather.is_up", return_value=False):
            assert entity.condition == "cloudy"


class TestForecastTwiceDaily:
    async def test_returns_single_condition_only_entry(self, hass):
        entity = make_entity(hass, {"ForecastIcon": 6})
        with patch("custom_components.davis_vantage.weather.is_up", return_value=True):
            forecast = await entity.async_forecast_twice_daily()
        assert forecast is not None
        assert len(forecast) == 1
        assert forecast[0]["condition"] == "partlycloudy"
        assert forecast[0]["is_daytime"] is True

    async def test_returns_none_when_no_coordinator_data(self, hass):
        entity = make_entity(hass, {})
        entity.coordinator.data = None
        assert await entity.async_forecast_twice_daily() is None


class TestNativeApparentTemperature:
    def test_prefers_thsw_when_present(self, hass):
        entity = make_entity(hass, {"THSWIndex": 95, "HeatIndex": 90})
        assert entity.native_apparent_temperature == 95

    def test_falls_back_to_heat_index_when_thsw_absent(self, hass):
        entity = make_entity(hass, {"THSWIndex": None, "HeatIndex": 90})
        assert entity.native_apparent_temperature == 90


class TestNativePressure:
    def test_valid_range_passes_through(self, hass):
        entity = make_entity(hass, {"Barometer": 29.92})
        assert entity.native_pressure == 29.92

    @pytest.mark.parametrize("bad", [0, 19.9, 32.6, 100])
    def test_out_of_physical_range_is_none(self, hass, bad):
        entity = make_entity(hass, {"Barometer": bad})
        assert entity.native_pressure is None

    def test_missing_is_none(self, hass):
        entity = make_entity(hass, {})
        assert entity.native_pressure is None


class TestNativeWindGustSpeed:
    def test_returns_wind_gust(self, hass):
        entity = make_entity(hass, {"WindGust": 22.5})
        assert entity.native_wind_gust_speed == 22.5

    def test_dash_sentinel_is_none(self, hass):
        entity = make_entity(hass, {"WindGust": 255})
        assert entity.native_wind_gust_speed is None

    def test_missing_coordinator_data_is_none(self, hass):
        entity = make_entity(hass, {"WindGust": 10})
        entity.coordinator.data = None
        assert entity.native_wind_gust_speed is None


class TestUvIndex:
    def test_returns_uv_value(self, hass):
        entity = make_entity(hass, {"UV": 4.2})
        assert entity.uv_index == 4.2

    def test_missing_optional_uv_sensor_is_none(self, hass):
        entity = make_entity(hass, {})
        assert entity.uv_index is None
