"""Tests for DavisServicesSetup's service handlers.

Handlers are invoked directly with a fake ServiceCall (a plain object with
a `.data` dict) rather than through hass.services.async_call - the thing
worth testing here is each handler's own logic (response shaping, the
archive-period cache-clear wiring), not HA's service-dispatch machinery.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.davis_vantage.client import DavisVantageClient
from custom_components.davis_vantage.const import DOMAIN, PROTOCOL_SERIAL
from custom_components.davis_vantage.services import DavisServicesSetup


def make_services(client) -> DavisServicesSetup:
    coordinator = SimpleNamespace(client=client)
    config_entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinator=coordinator))
    hass = MagicMock()
    hass.config.time_zone = "UTC"
    services = DavisServicesSetup.__new__(DavisServicesSetup)
    services.hass = hass
    services.config_entry = config_entry
    return services


def make_call(**data) -> SimpleNamespace:
    return SimpleNamespace(data=data)


class TestGetDavisTime:
    async def test_returns_iso_time_when_available(self):
        import datetime

        client = MagicMock()
        client.async_get_davis_time = AsyncMock(
            return_value=datetime.datetime(2026, 8, 20, 12, 0, 0)
        )
        services = make_services(client)

        result = await services.get_davis_time(make_call())

        assert "davis_time" in result

    async def test_returns_error_dict_when_unavailable(self):
        client = MagicMock()
        client.async_get_davis_time = AsyncMock(return_value=None)
        services = make_services(client)

        result = await services.get_davis_time(make_call())

        assert "error" in result


class TestGetInfo:
    async def test_returns_info_when_available(self):
        client = MagicMock()
        client.async_get_info = AsyncMock(return_value={"version": "1.0"})
        services = make_services(client)

        result = await services.get_info(make_call())

        assert result == {"version": "1.0"}

    async def test_returns_error_dict_when_unavailable(self):
        client = MagicMock()
        client.async_get_info = AsyncMock(return_value=None)
        services = make_services(client)

        result = await services.get_info(make_call())

        assert "error" in result


class TestGetRawData:
    async def test_converts_bytes_fields_to_hex(self):
        client = MagicMock()
        client.get_raw_data.return_value = {"_raw_bytes": b"\x01\x02", "Foo": 42}
        client.get_raw_hilows.return_value = {"Bar": "baz"}
        services = make_services(client)

        result = await services.get_raw_data(make_call())

        assert result["_raw_bytes"] == "01 02"
        assert result["Foo"] == 42
        assert result["Bar"] == "baz"


class TestSetArchivePeriod:
    async def test_clears_cache_even_when_never_previously_cached(self):
        # Regression test: clear_cached_property used to do
        # `del self._vantagepro2.__dict__[property_name]`, which raised
        # KeyError if archive_period had never been accessed yet (e.g. right
        # after setup, before any successful poll primed the cache) - the
        # service call would crash instead of just... setting the period.
        client = DavisVantageClient(
            hass=None, protocol=PROTOCOL_SERIAL, link="/dev/ttyUSB0", persistent_connection=False
        )
        # A fresh MagicMock's __dict__ has no "archive_period" key until
        # something explicitly sets it - i.e. exactly the "never cached" case.
        client._vantagepro2 = MagicMock()
        client.set_archive_period = MagicMock()
        services = make_services(client)

        await services.set_archive_period(make_call(archive_period="15"))

        client.set_archive_period.assert_called_once_with("15")

    async def test_clears_cache_when_no_connection_established_yet(self):
        client = DavisVantageClient(
            hass=None, protocol=PROTOCOL_SERIAL, link="/dev/ttyUSB0", persistent_connection=False
        )
        assert client._vantagepro2 is None
        client.set_archive_period = MagicMock()
        services = make_services(client)

        # Must not raise even though there's nothing to clear yet.
        await services.set_archive_period(make_call(archive_period="5"))


class TestSetBarometerCalibration:
    async def test_passes_elevation_and_barometer_through(self):
        client = MagicMock()
        client.async_set_barometer_calibration = AsyncMock(return_value="OK\n")
        services = make_services(client)

        await services.set_barometer_calibration(
            make_call(elevation=500, barometer=29.92)
        )

        client.async_set_barometer_calibration.assert_called_once_with(500, 29.92)

    async def test_defaults_barometer_to_zero_when_omitted(self):
        client = MagicMock()
        client.async_set_barometer_calibration = AsyncMock(return_value="OK\n")
        services = make_services(client)

        await services.set_barometer_calibration(make_call(elevation=500))

        client.async_set_barometer_calibration.assert_called_once_with(500, 0.0)


class TestEepromServices:
    async def test_get_eeprom_returns_data_key(self):
        client = MagicMock()
        client.async_get_eeprom = AsyncMock(return_value="deadbeef")
        services = make_services(client)

        result = await services.get_eeprom(make_call(address="0B", size=6))

        assert result == {"data": "deadbeef"}
        client.async_get_eeprom.assert_called_once_with("0B", 6)

    async def test_set_eeprom_passes_address_and_data(self):
        client = MagicMock()
        client.async_set_eeprom = AsyncMock()
        services = make_services(client)

        await services.set_eeprom(make_call(address="0B", data="deadbeef"))

        client.async_set_eeprom.assert_called_once_with("0B", "deadbeef")


class TestConsoleServices:
    async def test_set_console_lamps_passes_state(self):
        client = MagicMock()
        client.async_set_console_lamps = AsyncMock()
        services = make_services(client)

        await services.set_console_lamps(make_call(state=True))

        client.async_set_console_lamps.assert_called_once_with(True)

    async def test_clear_alarms_calls_client(self):
        client = MagicMock()
        client.async_clear_alarms = AsyncMock()
        services = make_services(client)

        await services.clear_alarms(make_call())

        client.async_clear_alarms.assert_called_once()


class TestMultipleEntryRouting:
    def test_entry_id_selects_the_requested_transport_owner(self):
        first_client = MagicMock()
        second_client = MagicMock()
        first = SimpleNamespace(
            entry_id="first",
            domain=DOMAIN,
            runtime_data=SimpleNamespace(
                coordinator=SimpleNamespace(client=first_client)
            ),
        )
        second = SimpleNamespace(
            entry_id="second",
            domain=DOMAIN,
            runtime_data=SimpleNamespace(
                coordinator=SimpleNamespace(client=second_client)
            ),
        )
        hass = MagicMock()
        hass.config_entries.async_get_entry.return_value = second
        hass.config_entries.async_entries.return_value = [first, second]
        services = DavisServicesSetup.__new__(DavisServicesSetup)
        services.hass = hass
        services.config_entry = None

        assert services._client_for_call(make_call(entry_id="second")) is second_client
        hass.config_entries.async_get_entry.assert_called_once_with("second")

    def test_multiple_entries_require_an_explicit_entry_id(self):
        hass = MagicMock()
        hass.config_entries.async_entries.return_value = [MagicMock(), MagicMock()]
        services = DavisServicesSetup.__new__(DavisServicesSetup)
        services.hass = hass
        services.config_entry = None

        with pytest.raises(ValueError, match="entry_id is required"):
            services._entry_for_call(make_call())

