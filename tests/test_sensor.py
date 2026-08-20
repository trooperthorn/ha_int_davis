"""Tests for davis_vantage.sensor helper functions."""
import pytest

from custom_components.davis_vantage.sensor import (
    _barometer_or_none,
    _float_or_none,
    _map_forecast_icon,
    _wind_dir_or_none,
    get_wind_rose,
)


class TestFloatOrNone:
    def test_present_value_converted(self):
        assert _float_or_none({"TempOut": "72.5"}, "TempOut") == 72.5

    def test_missing_key_is_none(self):
        assert _float_or_none({}, "TempOut") is None

    def test_none_value_is_none(self):
        assert _float_or_none({"TempOut": None}, "TempOut") is None


class TestWindDirOrNone:
    def test_valid_bearing(self):
        assert _wind_dir_or_none({"WindDir": 180}) == 180.0

    @pytest.mark.parametrize("dashed", [None, 0, 32767])
    def test_calm_or_dashed_is_none(self, dashed):
        assert _wind_dir_or_none({"WindDir": dashed}) is None

    def test_missing_key_is_none(self):
        assert _wind_dir_or_none({}) is None


class TestBarometerOrNone:
    def test_valid_reading_passes_through(self):
        assert _barometer_or_none({"Barometer": 29.91}) == 29.91

    @pytest.mark.parametrize("bad", [0.0, 19.99, 32.51, 100.0])
    def test_out_of_physical_range_is_none(self, bad):
        assert _barometer_or_none({"Barometer": bad}) is None

    def test_missing_key_is_none(self):
        assert _barometer_or_none({}) is None


class TestMapForecastIcon:
    def test_known_icon_maps(self):
        mapping = {8: "sunny", 2: "cloudy"}
        assert _map_forecast_icon({"ForecastIcon": 8}, mapping, "default") == "sunny"

    def test_unknown_icon_uses_default(self):
        mapping = {8: "sunny"}
        assert _map_forecast_icon({"ForecastIcon": 999}, mapping, "default") == "default"

    @pytest.mark.parametrize("dashed", [None, "", 255, 32767, -32768])
    def test_dashed_values_use_default(self, dashed):
        mapping = {8: "sunny"}
        assert _map_forecast_icon({"ForecastIcon": dashed}, mapping, "default") == "default"

    def test_non_numeric_value_does_not_raise(self):
        mapping = {8: "sunny"}
        assert _map_forecast_icon({"ForecastIcon": "garbage"}, mapping, "default") == "default"


class TestGetWindRoseAcceptsOptional:
    def test_none_passthrough_is_none(self):
        assert get_wind_rose(None) is None

    def test_typical_bearing(self):
        assert get_wind_rose(90.0) == "E"
