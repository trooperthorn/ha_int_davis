"""Integration tests for DavisVantageConfigFlow and its options flow.

These exercise the flow against a real Home Assistant instance via
pytest-homeassistant-custom-component, patching out the two things that
would otherwise touch real hardware: pyserial (port enumeration + the
fast wake/LOOP2 probe) and DavisVantageClient's connection methods.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.davis_vantage.const import (
    CONFIG_BAUD_RATE,
    CONFIG_INTERVAL,
    CONFIG_LINK,
    CONFIG_PROTOCOL,
    DOMAIN,
    PROTOCOL_NETWORK,
    PROTOCOL_SERIAL,
)

pytestmark = pytest.mark.asyncio


def _no_ports():
    return []


async def test_user_step_shows_protocol_choice(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"


async def test_serial_probe_success_flows_to_other_info_and_saves_serial_protocol(hass):
    # Regression test: async_step_setup_other_info used to hardcode
    # CONFIG_PROTOCOL = "Serial" no matter which path was taken. This nails
    # down the serial path's expected (and previously also-correct-by-luck)
    # outcome so a future regression on either path is caught.
    mock_ser = MagicMock()
    mock_ser.read.side_effect = [b"\n\r", b"\x06"]  # wake ACK, then LOOP2 ACK
    mock_serial_ctx = MagicMock()
    mock_serial_ctx.__enter__.return_value = mock_ser

    with patch(
        "serial.tools.list_ports.comports", side_effect=_no_ports
    ), patch("serial.Serial", return_value=mock_serial_ctx):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_PROTOCOL: PROTOCOL_SERIAL}
        )
        assert result["step_id"] == "setup_serial"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_LINK: "/dev/ttyUSB0"}
        )
        assert result["step_id"] == "setup_other_info"
        # LOOP2 was detected by the probe, so the form should default it on.
        assert result["data_schema"]({}).get("use_loop2") is True

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_INTERVAL: 300, "use_loop2": True}
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONFIG_PROTOCOL] == PROTOCOL_SERIAL
    assert result["data"][CONFIG_LINK] == "/dev/ttyUSB0"
    assert result["data"][CONFIG_BAUD_RATE] == 19200


async def test_serial_probe_failure_shows_error_and_stays_on_form(hass):
    mock_ser = MagicMock()
    mock_ser.read.return_value = b""  # no ACK at any baud rate
    mock_serial_ctx = MagicMock()
    mock_serial_ctx.__enter__.return_value = mock_ser

    with patch(
        "serial.tools.list_ports.comports", side_effect=_no_ports
    ), patch("serial.Serial", return_value=mock_serial_ctx):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_PROTOCOL: PROTOCOL_SERIAL}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_LINK: "/dev/ttyUSB0"}
        )

    assert result["type"] == "form"
    assert result["step_id"] == "setup_serial"
    assert result["errors"] == {"base": "no_davis_device"}


async def test_network_setup_saves_network_protocol_not_serial(hass):
    # Regression test for the hardcoded-"Serial" bug: a station configured
    # over the network path must end up with CONFIG_PROTOCOL == "Network",
    # otherwise get_link() builds a "serial:" URL for what the user told it
    # was a TCP connection.
    with patch(
        "custom_components.davis_vantage.config_flow.DavisVantageClient.connect_to_station",
        new=AsyncMock(return_value=None),
    ), patch(
        "custom_components.davis_vantage.config_flow.DavisVantageClient.async_get_davis_time",
        new=AsyncMock(return_value=object()),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_PROTOCOL: PROTOCOL_NETWORK}
        )
        assert result["step_id"] == "setup_network"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_LINK: "192.168.1.50:22222"}
        )
        assert result["step_id"] == "setup_other_info"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_INTERVAL: 300, "use_loop2": False}
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONFIG_PROTOCOL] == PROTOCOL_NETWORK
    assert result["data"][CONFIG_LINK] == "192.168.1.50:22222"


async def test_network_setup_validation_failure_shows_error(hass):
    with patch(
        "custom_components.davis_vantage.config_flow.DavisVantageClient.connect_to_station",
        new=AsyncMock(side_effect=ConnectionError("no route to host")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_PROTOCOL: PROTOCOL_NETWORK}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_LINK: "10.0.0.5:22222"}
        )

    assert result["type"] == "form"
    assert result["step_id"] == "setup_network"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_flow_round_trip(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONFIG_PROTOCOL: PROTOCOL_SERIAL, CONFIG_LINK: "/dev/ttyUSB0"},
        options={},
        title="Davis Vantage (test)",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONFIG_INTERVAL: 60, "use_loop2": True}
    )

    assert result["type"] == "create_entry"
    assert result["data"] == {CONFIG_INTERVAL: 60, "use_loop2": True}
