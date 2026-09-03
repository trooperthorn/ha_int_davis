"""Tests for DavisVantageSensor - native_value's dash-masking/retention/
fallback logic and entity_registry_enabled_default's optional-sensor gating.

Separate from test_sensor.py, which covers the module's pure helper
functions (_float_or_none, get_wind_rose, etc.) rather than the entity
class itself.
"""
from unittest.mock import MagicMock

import pytest

from custom_components.davis_vantage.sensor import SENSOR_TYPES, DavisVantageSensor


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.device_info = MagicMock()


def make_sensor(data, key):
    description = next(d for d in SENSOR_TYPES if d.key == key)
    return DavisVantageSensor(
        coordinator=_FakeCoordinator(data), entry_id="entry123", description=description
    )


class TestNativeValueDashMasking:
    def test_normal_value_passes_through(self):
        sensor = make_sensor({"TempOut": 72.5}, key="TempOut")
        assert sensor.native_value == 72.5

    @pytest.mark.parametrize("sentinel", [255, 32767, 32768, -32768])
    def test_dash_sentinel_becomes_none_for_generic_sensor(self, sentinel):
        sensor = make_sensor({"ConsoleBatteryVoltage": sentinel}, key="console_battery")
        assert sensor.native_value is None

    def test_missing_coordinator_data_is_none(self):
        sensor = make_sensor(None, key="TempOut")
        assert sensor.native_value is None


class TestRainRateFallback:
    def test_valid_rate_passes_through(self):
        sensor = make_sensor({"RainRate": 0.5}, key="rain_rate")
        assert sensor.native_value == 0.5

    @pytest.mark.parametrize("sentinel", [255, 32767, -32768])
    def test_dash_sentinel_falls_back_to_zero_not_none(self, sentinel):
        # Rain rate specifically must show 0, not Unknown, when the console
        # hasn't reported a value yet - Unknown would break automations
        # that gate on "is it raining right now".
        sensor = make_sensor({"RainRate": sentinel}, key="rain_rate")
        assert sensor.native_value == 0


class TestWindDirectionRetention:
    def test_calm_air_with_no_history_returns_none(self):
        sensor = make_sensor({"WindDir": 0}, key="wind_direction")
        assert sensor.native_value is None

    def test_calm_air_retains_previous_reading(self):
        sensor = make_sensor({"WindDir": 270}, key="wind_direction")
        assert sensor.native_value == 270

        # Console reports calm (0) on the next poll - should hold the last
        # known direction rather than snapping to 0 or Unknown.
        sensor.coordinator.data = {"WindDir": 0}
        assert sensor.native_value == 270

    def test_dash_sentinel_with_history_retains_previous(self):
        sensor = make_sensor({"WindDir": 90}, key="wind_direction")
        assert sensor.native_value == 90

        sensor.coordinator.data = {"WindDir": 32767}
        assert sensor.native_value == 90


class TestMissingValueLogging:
    """native_value() warns on a None reading, but only once per outage.

    Previously it warned on every single poll a sensor stayed None, which for
    an always-enabled sensor fed only by a best-effort fetch (e.g. one that
    depends on the archive round trip) meant unsuppressable per-poll WARNING
    log spam for as long as the station withheld that value.
    """

    def test_warns_once_then_stays_quiet_while_still_missing(self, caplog):
        import logging

        sensor = make_sensor({"ConsoleBatteryVoltage": 255}, key="console_battery")
        with caplog.at_level(logging.WARNING):
            assert sensor.native_value is None
            assert sensor.native_value is None
            assert sensor.native_value is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_warns_again_after_recovering_then_missing_again(self, caplog):
        import logging

        sensor = make_sensor({"ConsoleBatteryVoltage": 255}, key="console_battery")
        with caplog.at_level(logging.WARNING):
            assert sensor.native_value is None
            sensor.coordinator.data = {"ConsoleBatteryVoltage": 4.9}
            assert sensor.native_value == 4.9
            sensor.coordinator.data = {"ConsoleBatteryVoltage": 255}
            assert sensor.native_value is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2


class TestOptionalSensorEnabledDefault:
    def test_disabled_when_no_coordinator_data_yet(self):
        sensor = make_sensor(None, key="uv_index")
        assert sensor.entity_registry_enabled_default is False

    def test_disabled_when_initial_value_is_dash_sentinel(self):
        sensor = make_sensor({"UV": 255}, key="uv_index")
        assert sensor.entity_registry_enabled_default is False

    def test_enabled_when_real_hardware_value_present(self):
        sensor = make_sensor({"UV": 3.2}, key="uv_index")
        assert sensor.entity_registry_enabled_default is True

    def test_solar_radiation_follows_same_gating(self):
        sensor = make_sensor({"SolarRad": 450}, key="solar_radiation")
        assert sensor.entity_registry_enabled_default is True

    def test_non_optional_sensor_always_enabled(self):
        sensor = make_sensor(None, key="TempOut")
        assert sensor.entity_registry_enabled_default is True
