"""Read-only endpoint verification tests. Serial/USB is the only transport."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.davis_vantage.const import IDENTITY_WEAK, PROTOCOL_SERIAL
from custom_components.davis_vantage.verification import (
    DavisCannotConnectError,
    canonicalize_serial_endpoint,
    verify_serial,
)


def test_windows_com_path_is_preserved_case_insensitively():
    with patch("custom_components.davis_vantage.verification.os.name", "nt"):
        assert canonicalize_serial_endpoint("com7") == "COM7"


def test_empty_serial_endpoint_is_a_user_error():
    with pytest.raises(DavisCannotConnectError):
        canonicalize_serial_endpoint("   ")


def test_serial_probe_opens_only_submitted_endpoint_and_closes_it():
    transport = MagicMock()
    with patch(
        "serialx.serial_for_url", return_value=transport
    ) as serial_for_url, patch(
        "custom_components.davis_vantage.verification._wake", return_value=True
    ), patch(
        "custom_components.davis_vantage.verification._probe_loop2",
        return_value=True,
    ), patch("serialx.list_serial_ports", return_value=[]):
        result = verify_serial("socket://logger.example:2000")

    serial_for_url.assert_called_once_with(
        "socket://logger.example:2000",
        baudrate=19200,
        timeout=2.0,
        write_timeout=2.0,
    )
    transport.open.assert_called_once_with()
    transport.close.assert_called_once()
    assert result.protocol == PROTOCOL_SERIAL
    assert result.identity_strength == IDENTITY_WEAK
    assert result.loop2_supported is True
