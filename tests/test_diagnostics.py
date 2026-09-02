"""Tests for davis_vantage.diagnostics - console-command degradation and
location redaction.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


from custom_components.davis_vantage.diagnostics import (
    _try_console_command,
    async_get_config_entry_diagnostics,
)


def make_config_entry(client, coordinator_data=None):
    coordinator = SimpleNamespace(client=client, data=coordinator_data or {})
    return SimpleNamespace(
        runtime_data=SimpleNamespace(coordinator=coordinator),
        data={"protocol": "Serial", "link": "/dev/ttyUSB0"},
        options={},
    )


def make_client(**overrides) -> MagicMock:
    client = MagicMock()
    client.async_get_rxcheck = AsyncMock(return_value="RXCHECK OK\n")
    client.async_get_nver = AsyncMock(return_value="1.90\n")
    client.async_get_bardata = AsyncMock(return_value="BAR 29.92\n")
    client.get_raw_data.return_value = {"_raw_bytes": b"\x01", "TempOut": 720}
    client.firmware_version = "1.90"
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


class TestTryConsoleCommand:
    async def test_returns_stripped_result_on_success(self):
        result = await _try_console_command(AsyncMock(return_value="  hello \n"))
        assert result == "hello"

    async def test_degrades_to_error_string_on_failure(self):
        func = AsyncMock(side_effect=OSError("port vanished"))
        result = await _try_console_command(func)
        assert "unavailable" in result
        assert "port vanished" in result


class TestAsyncGetConfigEntryDiagnostics:
    async def test_happy_path_includes_all_sections(self, hass):
        client = make_client()
        entry = make_config_entry(client, coordinator_data={"TempOut": 72})

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["console"]["rxcheck"] == "RXCHECK OK"
        assert result["console"]["firmware_version"] == "1.90"
        assert result["last_data"] == {"TempOut": 72}
        assert result["raw_loop_data"] == {"TempOut": 720}  # _raw_bytes stripped

    async def test_console_unreachable_does_not_crash_whole_dump(self, hass):
        # This is the actual scenario diagnostics get downloaded for - the
        # console being unreachable must not take out the config/last-data
        # sections along with the failed console round-trip.
        client = make_client()
        client.async_get_rxcheck = AsyncMock(side_effect=ConnectionError("no ACK"))
        client.async_get_nver = AsyncMock(side_effect=ConnectionError("no ACK"))
        client.async_get_bardata = AsyncMock(side_effect=ConnectionError("no ACK"))
        entry = make_config_entry(client, coordinator_data={"LastError": "no ACK"})

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert "unavailable" in result["console"]["rxcheck"]
        assert result["last_data"] == {"LastError": "no ACK"}
        assert result["config_entry"]["protocol"] == "Serial"

    async def test_redacts_latitude_and_longitude_from_last_data(self, hass):
        client = make_client()
        entry = make_config_entry(
            client, coordinator_data={"Latitude": 40.0, "Longitude": -74.0, "TempOut": 72}
        )

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["last_data"]["Latitude"] == "**REDACTED**"
        assert result["last_data"]["Longitude"] == "**REDACTED**"
        assert result["last_data"]["TempOut"] == 72

    async def test_link_address_is_not_included(self, hass):
        # The port/host:port isn't personal data, but it's also not needed -
        # confirm it's deliberately left out of the config_entry section.
        client = make_client()
        entry = make_config_entry(client)

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert "link" not in result["config_entry"]

