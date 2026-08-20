"""Tests for davis_vantage/__init__.py's setup/unload lifecycle.

Focused on the connection-cleanup paths - both used to be dead code (a
duck-typed check for close()/disconnect() methods that never existed on
DavisVantageClient), so a failed first refresh or an unload would silently
leak the open serial connection instead of releasing it.
"""
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.davis_vantage import async_setup_entry
from custom_components.davis_vantage.const import CONFIG_LINK, CONFIG_PROTOCOL, DOMAIN, PROTOCOL_SERIAL

pytestmark = pytest.mark.asyncio


def make_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONFIG_PROTOCOL: PROTOCOL_SERIAL, CONFIG_LINK: "/dev/ttyUSB0"},
        options={},
        title="Davis Vantage (test)",
    )
    entry.add_to_hass(hass)
    return entry


async def test_first_refresh_failure_closes_connection_before_retry(hass):
    entry = make_entry(hass)

    with patch(
        "custom_components.davis_vantage.DavisVantageClient.connect_to_station",
        new=AsyncMock(return_value=None),
    ), patch(
        "custom_components.davis_vantage.DavisVantageClient.get_station_info",
        new=AsyncMock(return_value=None),
    ), patch(
        "custom_components.davis_vantage.DavisVantageClient.async_close",
        new=AsyncMock(),
    ) as mock_close, patch(
        "custom_components.davis_vantage.DavisVantageDataUpdateCoordinator"
        ".async_config_entry_first_refresh",
        new=AsyncMock(side_effect=RuntimeError("console never ACKed")),
    ):
        with pytest.raises(Exception):
            await async_setup_entry(hass, entry)

        mock_close.assert_awaited_once()


async def test_successful_unload_closes_connection_and_clears_repair_issue(hass):
    entry = make_entry(hass)

    with patch(
        "custom_components.davis_vantage.DavisVantageClient.connect_to_station",
        new=AsyncMock(return_value=None),
    ), patch(
        "custom_components.davis_vantage.DavisVantageClient.get_station_info",
        new=AsyncMock(return_value=None),
    ), patch(
        "custom_components.davis_vantage.DavisVantageDataUpdateCoordinator"
        ".async_config_entry_first_refresh",
        new=AsyncMock(return_value=None),
    ), patch(
        "custom_components.davis_vantage.DavisVantageClient.async_close",
        new=AsyncMock(),
    ) as mock_close:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        mock_close.assert_awaited_once()
