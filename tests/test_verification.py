"""Read-only endpoint verification tests."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.davis_vantage.const import (
    IDENTITY_WEAK,
    PROTOCOL_NETWORK,
    PROTOCOL_SERIAL,
)
from custom_components.davis_vantage.verification import (
    DavisCannotConnectError,
    canonicalize_network_endpoint,
    canonicalize_serial_endpoint,
    split_network_endpoint,
    verify_network,
    verify_serial,
)


def test_network_endpoint_defaults_port_and_supports_ipv6():
    assert canonicalize_network_endpoint("WeatherLink.LOCAL") == (
        "weatherlink.local:22222"
    )
    assert split_network_endpoint("[2001:db8::5]:22223") == (
        "2001:db8::5",
        22223,
    )


@pytest.mark.parametrize("endpoint", ["host:not-a-port", "host:70000", "2001:db8::5"])
def test_invalid_network_endpoints_are_user_errors(endpoint):
    with pytest.raises(DavisCannotConnectError):
        split_network_endpoint(endpoint)


def test_windows_com_path_is_preserved_case_insensitively():
    with patch("custom_components.davis_vantage.verification.os.name", "nt"):
        assert canonicalize_serial_endpoint("com7") == "COM7"


def test_serial_probe_opens_only_submitted_endpoint_and_closes_it():
    transport = MagicMock()
    with patch(
        "serialx.serial_for_url", return_value=transport
    ) as serial_for_url, patch(
        "custom_components.davis_vantage.verification._wake", return_value=True
    ), patch(
        "custom_components.davis_vantage.verification._probe_loop2",
        return_value=True,
    ), patch("serial.tools.list_ports.comports", return_value=[]):
        result = verify_serial("socket://logger.example:2000")

    serial_for_url.assert_called_once_with(
        "socket://logger.example:2000",
        baudrate=19200,
        timeout=2.0,
        write_timeout=2.0,
    )
    transport.close.assert_called_once()
    assert result.protocol == PROTOCOL_SERIAL
    assert result.identity_strength == IDENTITY_WEAK
    assert result.loop2_supported is True


def test_network_probe_performs_davis_exchange_and_releases_socket():
    sock = MagicMock()
    with patch("socket.create_connection", return_value=sock) as connect, patch(
        "custom_components.davis_vantage.verification._wake", return_value=True
    ), patch(
        "custom_components.davis_vantage.verification._probe_loop2",
        return_value=False,
    ):
        result = verify_network("WeatherLink.local")

    connect.assert_called_once_with(("weatherlink.local", 22222), timeout=2.0)
    sock.close.assert_called_once()
    assert result.protocol == PROTOCOL_NETWORK
    assert result.endpoint == "weatherlink.local:22222"
    assert result.loop2_supported is False

