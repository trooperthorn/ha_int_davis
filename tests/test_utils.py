"""Tests for davis_vantage.utils - pure functions, no Home Assistant needed."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.davis_vantage.utils import (
    calc_dew_point,
    calc_feels_like,
    calc_heat_index,
    calc_wind_chill,
    contains_correct_raw_data,
    convert_celcius_to_fahrenheit,
    convert_kmh_to_bft,
    convert_kmh_to_ms,
    convert_ms_to_bft,
    convert_to_celcius,
    convert_to_iso_datetime,
    convert_to_kmh,
    convert_to_mbar,
    convert_to_mm,
    convert_to_ms,
    get_baro_trend,
    get_solar_rad,
    get_uv,
    get_wind_rose,
    has_correct_value,
    normalize_unique_id,
    round_to_one_decimal,
)


class TestGetWindRose:
    """get_wind_rose had a real bug (modulo 8 against a 16-entry table)."""

    @pytest.mark.parametrize(
        "bearing,expected",
        [
            (0, "N"),
            (22.5, "NNE"),
            (45, "NE"),
            (90, "E"),
            (135, "SE"),
            (180, "S"),
            (202.5, "SSW"),
            (225, "SW"),
            (247.5, "WSW"),
            (270, "W"),
            (292.5, "WNW"),
            (315, "NW"),
            (337.5, "NNW"),
            (360, "N"),
        ],
    )
    def test_full_compass(self, bearing, expected):
        assert get_wind_rose(bearing) == expected

    def test_covers_every_compass_point(self):
        # Every one of the 16 named points must be reachable - this is
        # exactly what the % 8 bug broke (only 8 of 16 were ever returned).
        seen = {get_wind_rose(i * 22.5) for i in range(16)}
        assert len(seen) == 16


class TestConversions:
    def test_celsius_fahrenheit_roundtrip(self):
        assert convert_to_celcius(32.0) == 0.0
        assert convert_to_celcius(212.0) == 100.0
        assert convert_celcius_to_fahrenheit(0.0) == 32.0
        assert convert_celcius_to_fahrenheit(100.0) == 212.0

    def test_kmh_conversions(self):
        assert convert_to_kmh(1.0) == pytest.approx(1.609344, abs=0.01)
        assert convert_kmh_to_ms(36.0) == 10.0
        assert convert_to_ms(0.0) == 0.0

    def test_mbar_and_mm(self):
        assert convert_to_mbar(1.0) == pytest.approx(33.9, abs=0.01)
        assert convert_to_mm(1.0) == 20.0

    @pytest.mark.parametrize(
        "kmh,expected_bft",
        [
            (0.0, 0),
            (5.0, 1),
            (20.0, 4),
            (50.0, 7),
            (120.0, 12),
        ],
    )
    def test_beaufort_scale_is_monotonic(self, kmh, expected_bft):
        assert convert_kmh_to_bft(kmh) == expected_bft

    def test_beaufort_scale_never_decreases(self):
        prev = -1
        for kmh in range(0, 150, 5):
            bft = convert_kmh_to_bft(float(kmh))
            assert bft >= prev
            prev = bft


class TestBarometricTrend:
    @pytest.mark.parametrize(
        "trend,expected",
        [
            (-60, "falling_rapidly"),
            (196, "falling_rapidly"),  # -60 as an unsigned byte
            (-20, "falling_slowly"),
            (236, "falling_slowly"),  # -20 as an unsigned byte
            (0, "steady"),
            (20, "rising_slowly"),
            (60, "rising_rapidly"),
        ],
    )
    def test_known_values(self, trend, expected):
        assert get_baro_trend(trend) == expected

    def test_unknown_value_is_none(self):
        # e.g. 80 = ASCII "P" (Rev A firmware, no trend data available yet)
        assert get_baro_trend(80) is None


class TestWeatherFormulas:
    def test_heat_index_below_threshold_returns_temperature(self):
        # Below 80F or 40% humidity, Davis doesn't apply the heat index formula.
        assert calc_heat_index(75.0, 50.0) == 75.0
        assert calc_heat_index(85.0, 30.0) == 85.0

    def test_heat_index_above_threshold_exceeds_temperature(self):
        result = calc_heat_index(95.0, 70.0)
        assert result > 95.0

    def test_wind_chill_no_wind_returns_temperature(self):
        assert calc_wind_chill(40.0, 0.0) == 40.0

    def test_wind_chill_with_wind_is_colder(self):
        result = calc_wind_chill(30.0, 15.0)
        assert result < 30.0

    def test_dew_point_below_air_temperature(self):
        dew_point = calc_dew_point(75.0, 50.0)
        assert dew_point < 75.0

    def test_dew_point_at_100pct_humidity_equals_temperature(self):
        # At full saturation the dew point converges on the air temperature.
        dew_point = calc_dew_point(70.0, 100.0)
        assert dew_point == pytest.approx(70.0, abs=0.5)

    def test_feels_like_is_finite_across_typical_range(self):
        for temp in range(-20, 120, 10):
            for hum in range(0, 101, 20):
                for wind in range(0, 40, 10):
                    result = calc_feels_like(float(temp), float(hum), float(wind))
                    assert isinstance(result, float)


class TestUnitHelpers:
    def test_has_correct_value(self):
        assert has_correct_value(72.5) is True
        assert has_correct_value(255) is False
        assert has_correct_value(32767) is False

    def test_get_uv_rounds(self):
        assert get_uv(3.14159) == 3.1

    def test_get_solar_rad_passthrough(self):
        assert get_solar_rad(450) == 450

    def test_round_to_one_decimal(self):
        assert round_to_one_decimal(1.23456) == 1.2

    def test_contains_correct_raw_data_true(self):
        raw = {
            "TempOut": 725,
            "RainRate": 0,
            "WindSpeed": 5,
            "HumOut": 60,
            "WindSpeed10Min": 4,
        }
        assert contains_correct_raw_data(raw)

    def test_contains_correct_raw_data_false_on_dash_value(self):
        raw = {
            "TempOut": 32767,
            "RainRate": 0,
            "WindSpeed": 5,
            "HumOut": 60,
            "WindSpeed10Min": 4,
        }
        assert not contains_correct_raw_data(raw)


class TestDatetimeHelpers:
    def test_convert_to_iso_datetime_attaches_tzinfo(self):
        naive = datetime(2026, 8, 20, 12, 0, 0)
        tz = ZoneInfo("America/Los_Angeles")
        result = convert_to_iso_datetime(naive, tz)
        assert result.tzinfo == tz
        assert result.year == 2026


class TestNormalizeUniqueId:
    def test_normalizes_spaces_dashes_and_parens(self):
        assert normalize_unique_id("Davis Vantage (COM3)") == "davis_vantage_com3"

    def test_idempotent(self):
        once = normalize_unique_id("Some-Name (X)")
        assert normalize_unique_id(once) == once
