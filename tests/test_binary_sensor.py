"""Tests for davis_vantage.binary_sensor - value_fn logic and entity is_on."""
import dataclasses
from unittest.mock import MagicMock

import pytest

from custom_components.davis_vantage.binary_sensor import (
    BINARY_SENSOR_TYPES,
    DavisVantageBinarySensor,
    _evaluate_is_raining,
    _evaluate_iss_connection,
    _evaluate_tx_battery,
)


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.device_info = MagicMock()


def make_entity(data, key="is_raining"):
    description = next(d for d in BINARY_SENSOR_TYPES if d.key == key)
    return DavisVantageBinarySensor(
        coordinator=_FakeCoordinator(data), entry_id="entry123", description=description
    )


class TestEvaluateIsRaining:
    def test_true_when_raining(self):
        assert _evaluate_is_raining({"IsRaining": True}) is True

    def test_false_when_not_raining(self):
        assert _evaluate_is_raining({"IsRaining": False}) is False

    def test_false_when_key_missing(self):
        assert _evaluate_is_raining({}) is False


class TestEvaluateTxBattery:
    def test_low_battery_is_true(self):
        assert _evaluate_tx_battery({"TransmitterBatteryStatus": 1}) is True

    def test_normal_battery_is_false(self):
        assert _evaluate_tx_battery({"TransmitterBatteryStatus": 0}) is False

    def test_missing_is_false(self):
        assert _evaluate_tx_battery({}) is False


class TestEvaluateIssConnection:
    def test_valid_temp_only_is_online(self):
        assert _evaluate_iss_connection({"TempOut": 72.5, "WindSpeed": None}) is True

    def test_valid_wind_only_is_online(self):
        assert _evaluate_iss_connection({"TempOut": None, "WindSpeed": 5}) is True

    def test_both_missing_is_offline(self):
        assert _evaluate_iss_connection({}) is False

    @pytest.mark.parametrize("sentinel", [255, 32767, 32768, -32768, ""])
    def test_dash_sentinels_treated_as_invalid(self, sentinel):
        assert (
            _evaluate_iss_connection({"TempOut": sentinel, "WindSpeed": sentinel})
            is False
        )


class TestBinarySensorIsOn:
    def test_reflects_value_fn_result(self):
        entity = make_entity({"IsRaining": True}, key="is_raining")
        assert entity.is_on is True

    def test_none_when_no_coordinator_data(self):
        entity = make_entity(None, key="is_raining")
        assert entity.is_on is None

    def test_swallows_value_fn_exceptions_as_false(self):
        entity = make_entity({"anything": 1}, key="iss_connection")
        # A value_fn that raises (pathological data shape, bad EEPROM read,
        # etc.) must degrade the entity state to False, not raise into HA.
        entity.entity_description = dataclasses.replace(
            entity.entity_description,
            value_fn=lambda data: (_ for _ in ()).throw(TypeError("boom")),
        )
        assert entity.is_on is False
