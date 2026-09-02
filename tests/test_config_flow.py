"""Config-flow and options-flow contract tests."""

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.helpers.selector import SerialPortSelector

from custom_components.davis_vantage.const import (
    CONFIG_BAUD_RATE,
    CONFIG_IDENTITY,
    CONFIG_IDENTITY_SOURCE,
    CONFIG_IDENTITY_STRENGTH,
    CONFIG_INTERVAL,
    CONFIG_LINK,
    CONFIG_LOOP2_SUPPORTED,
    CONFIG_PERSISTENT_CONNECTION,
    CONFIG_PROTOCOL,
    CONF_USE_LOOP2,
    DOMAIN,
    IDENTITY_STRONG,
    IDENTITY_WEAK,
    PROTOCOL_NETWORK,
    PROTOCOL_SERIAL,
)
from custom_components.davis_vantage.verification import (
    DavisNotFoundError,
    VerificationResult,
)

pytestmark = pytest.mark.asyncio


def _verified(
    protocol: str,
    endpoint: str,
    *,
    identity: str = "usb:1234:5678:console-a",
    strength: str = IDENTITY_STRONG,
    loop2: bool = True,
    baud: int | None = 19200,
) -> VerificationResult:
    return VerificationResult(
        protocol=protocol,
        endpoint=endpoint,
        identity=identity,
        identity_source=(
            "usb_serial_number" if strength == IDENTITY_STRONG else "canonical_endpoint"
        ),
        identity_strength=strength,
        loop2_supported=loop2,
        baud_rate=baud if protocol == PROTOCOL_SERIAL else None,
    )


async def _start_user_flow(hass, protocol: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONFIG_PROTOCOL: protocol}
    )


async def test_interface_form_uses_native_selector_without_probing(hass):
    with patch(
        "custom_components.davis_vantage.config_flow.verify_connection"
    ) as verify:
        result = await _start_user_flow(hass, PROTOCOL_SERIAL)

    assert result["step_id"] == "interface"
    assert isinstance(next(iter(result["data_schema"].schema.values())), SerialPortSelector)
    verify.assert_not_called()


async def test_serial_flow_verifies_then_separates_data_and_options(hass):
    verification = _verified(PROTOCOL_SERIAL, "/dev/serial/by-id/davis-a")
    with patch(
        "custom_components.davis_vantage.config_flow.verify_connection",
        return_value=verification,
    ) as verify:
        result = await _start_user_flow(hass, PROTOCOL_SERIAL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_LINK: "/dev/ttyUSB0"}
        )
        assert result["step_id"] == "verify"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "options"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONFIG_INTERVAL: 60,
                CONF_USE_LOOP2: True,
                CONFIG_PERSISTENT_CONNECTION: True,
            },
        )

    verify.assert_called_once_with(PROTOCOL_SERIAL, "/dev/ttyUSB0")
    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONFIG_PROTOCOL: PROTOCOL_SERIAL,
        CONFIG_LINK: "/dev/serial/by-id/davis-a",
        CONFIG_IDENTITY: "usb:1234:5678:console-a",
        CONFIG_IDENTITY_SOURCE: "usb_serial_number",
        CONFIG_IDENTITY_STRENGTH: IDENTITY_STRONG,
        CONFIG_LOOP2_SUPPORTED: True,
        CONFIG_BAUD_RATE: 19200,
    }
    assert result["options"] == {
        CONFIG_INTERVAL: 60,
        CONF_USE_LOOP2: True,
        CONFIG_PERSISTENT_CONNECTION: True,
    }


async def test_network_flow_preserves_weatherlink_transport(hass):
    verification = _verified(
        PROTOCOL_NETWORK,
        "weatherlink.local:22222",
        identity="network:weatherlink.local:22222",
        strength=IDENTITY_WEAK,
        loop2=False,
    )
    with patch(
        "custom_components.davis_vantage.config_flow.verify_connection",
        return_value=verification,
    ):
        result = await _start_user_flow(hass, PROTOCOL_NETWORK)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_LINK: "WeatherLink.local"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONFIG_INTERVAL: 300,
                CONF_USE_LOOP2: False,
                CONFIG_PERSISTENT_CONNECTION: True,
            },
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONFIG_PROTOCOL] == PROTOCOL_NETWORK
    assert result["data"][CONFIG_LINK] == "weatherlink.local:22222"
    assert CONFIG_BAUD_RATE not in result["data"]


async def test_only_submitted_interface_failure_returns_to_selector(hass):
    with patch(
        "custom_components.davis_vantage.config_flow.verify_connection",
        side_effect=DavisNotFoundError,
    ) as verify:
        result = await _start_user_flow(hass, PROTOCOL_SERIAL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_LINK: "socket://manual.example:2000"}
        )

    verify.assert_called_once_with(PROTOCOL_SERIAL, "socket://manual.example:2000")
    assert result["step_id"] == "interface"
    assert result["errors"] == {"base": "no_davis_device"}


async def test_duplicate_strong_console_is_rejected(hass):
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="davis:usb:1234:5678:console-a",
        data={
            CONFIG_PROTOCOL: PROTOCOL_SERIAL,
            CONFIG_LINK: "/dev/serial/by-id/davis-a",
            CONFIG_IDENTITY: "usb:1234:5678:console-a",
        },
        options={},
        version=2,
    )
    existing.add_to_hass(hass)

    with patch(
        "custom_components.davis_vantage.config_flow.verify_connection",
        return_value=_verified(PROTOCOL_SERIAL, "COM7"),
    ):
        result = await _start_user_flow(hass, PROTOCOL_SERIAL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_LINK: "COM7"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONFIG_INTERVAL: 30,
                CONF_USE_LOOP2: True,
                CONFIG_PERSISTENT_CONNECTION: False,
            },
        )

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_reconfigure_rejects_a_different_physical_console(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="davis:usb:1234:5678:console-a",
        data={
            CONFIG_PROTOCOL: PROTOCOL_SERIAL,
            CONFIG_LINK: "COM3",
            CONFIG_IDENTITY: "usb:1234:5678:console-a",
            CONFIG_IDENTITY_SOURCE: "usb_serial_number",
            CONFIG_IDENTITY_STRENGTH: IDENTITY_STRONG,
            CONFIG_LOOP2_SUPPORTED: True,
            CONFIG_BAUD_RATE: 19200,
        },
        options={CONFIG_INTERVAL: 30},
        version=2,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        data=entry.data,
    )
    if result["step_id"] == "reconfigure":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_PROTOCOL: PROTOCOL_SERIAL}
        )
    assert result["step_id"] == "interface"
    with patch(
        "custom_components.davis_vantage.config_flow.verify_connection",
        return_value=_verified(
            PROTOCOL_SERIAL,
            "COM4",
            identity="usb:1234:5678:console-b",
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_LINK: "COM4"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == "abort"
    assert result["reason"] == "wrong_device"
    assert entry.data[CONFIG_LINK] == "COM3"


async def test_network_reconfigure_updates_once_and_preserves_options(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="davis:generated:entry-a",
        data={
            CONFIG_PROTOCOL: PROTOCOL_SERIAL,
            CONFIG_LINK: "COM3",
            CONFIG_IDENTITY: "generated:entry-a",
            CONFIG_IDENTITY_SOURCE: "generated",
            CONFIG_IDENTITY_STRENGTH: IDENTITY_WEAK,
            CONFIG_LOOP2_SUPPORTED: False,
            CONFIG_BAUD_RATE: 19200,
        },
        options={
            CONFIG_INTERVAL: 60,
            CONF_USE_LOOP2: False,
            CONFIG_PERSISTENT_CONNECTION: True,
        },
        version=2,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONFIG_PROTOCOL: PROTOCOL_NETWORK}
    )
    with patch(
        "custom_components.davis_vantage.config_flow.verify_connection",
        return_value=_verified(
            PROTOCOL_NETWORK,
            "logger.local:22222",
            identity="network:logger.local:22222",
            strength=IDENTITY_WEAK,
            loop2=True,
        ),
    ), patch.object(hass.config_entries, "async_reload") as reload_entry:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONFIG_LINK: "LOGGER.local"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONFIG_PROTOCOL] == PROTOCOL_NETWORK
    assert entry.data[CONFIG_LINK] == "logger.local:22222"
    assert CONFIG_BAUD_RATE not in entry.data
    assert entry.options[CONFIG_INTERVAL] == 60
    reload_entry.assert_not_called()


async def test_options_flow_owns_all_runtime_tuning_values(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONFIG_PROTOCOL: PROTOCOL_SERIAL,
            CONFIG_LINK: "COM3",
            CONFIG_LOOP2_SUPPORTED: True,
        },
        options={},
        version=2,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONFIG_INTERVAL: 60,
            CONF_USE_LOOP2: True,
            CONFIG_PERSISTENT_CONNECTION: True,
        },
    )

    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONFIG_INTERVAL: 60,
        CONF_USE_LOOP2: True,
        CONFIG_PERSISTENT_CONNECTION: True,
    }
