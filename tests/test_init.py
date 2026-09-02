"""Tests for davis_vantage/__init__.py's setup/unload lifecycle.

Focused on the connection-cleanup paths - both used to be dead code (a
duck-typed check for close()/disconnect() methods that never existed on
DavisVantageClient), so a failed first refresh or an unload would silently
leak the open serial connection instead of releasing it.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.davis_vantage import async_migrate_entry, async_setup_entry
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
    IDENTITY_WEAK,
    PROTOCOL_SERIAL,
)

pytestmark = pytest.mark.asyncio


def make_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONFIG_PROTOCOL: PROTOCOL_SERIAL, CONFIG_LINK: "/dev/ttyUSB0"},
        options={},
        title="Davis Vantage (test)",
        version=2,
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


async def test_options_update_uses_the_single_reload_listener(hass):
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            CONFIG_PROTOCOL: PROTOCOL_SERIAL,
            CONFIG_LINK: "COM3",
            CONFIG_IDENTITY: "generated:test",
            CONFIG_IDENTITY_SOURCE: "generated",
            CONFIG_IDENTITY_STRENGTH: IDENTITY_WEAK,
            CONFIG_LOOP2_SUPPORTED: False,
            CONFIG_BAUD_RATE: 19200,
        },
    )

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
    ), patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as reload_entry:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        hass.config_entries.async_update_entry(
            entry,
            options={
                CONFIG_INTERVAL: 90,
                CONF_USE_LOOP2: False,
                CONFIG_PERSISTENT_CONNECTION: True,
            },
        )
        await hass.async_block_till_done()

    reload_entry.assert_awaited_once_with(entry.entry_id)


async def test_legacy_entry_migration_removes_duplicate_option_owners(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONFIG_PROTOCOL: PROTOCOL_SERIAL,
            CONFIG_LINK: "COM3",
            CONFIG_INTERVAL: 120,
            CONF_USE_LOOP2: True,
            CONFIG_PERSISTENT_CONNECTION: True,
        },
        options={},
        version=1,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 2
    assert CONFIG_INTERVAL not in entry.data
    assert CONF_USE_LOOP2 not in entry.data
    assert CONFIG_PERSISTENT_CONNECTION not in entry.data
    assert entry.options == {
        CONFIG_INTERVAL: 120,
        CONF_USE_LOOP2: True,
        CONFIG_PERSISTENT_CONNECTION: True,
    }
    assert entry.unique_id is not None
    assert entry.data[CONFIG_LINK] == "COM3"


async def test_immediate_reload_waits_for_prior_client_close(hass):
    entry = make_entry(hass)
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def delayed_close() -> bool:
        close_started.set()
        await allow_close.wait()
        return True

    first_client = MagicMock()
    first_client.connect_to_station = AsyncMock()
    first_client.get_station_info = AsyncMock()
    first_client.async_begin_shutdown = AsyncMock()
    first_client.async_close = AsyncMock(side_effect=delayed_close)
    first_client.firmware_version = None

    second_client = MagicMock()
    second_client.connect_to_station = AsyncMock()
    second_client.get_station_info = AsyncMock()
    second_client.async_begin_shutdown = AsyncMock()
    second_client.async_close = AsyncMock(return_value=True)
    second_client.firmware_version = None

    with patch(
        "custom_components.davis_vantage.DavisVantageClient",
        side_effect=[first_client, second_client],
    ) as client_factory, patch(
        "custom_components.davis_vantage.DavisVantageDataUpdateCoordinator"
        ".async_config_entry_first_refresh",
        new=AsyncMock(return_value=None),
    ), patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        new=AsyncMock(return_value=None),
    ), patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=True),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        reload_task = asyncio.create_task(
            hass.config_entries.async_reload(entry.entry_id)
        )
        await close_started.wait()
        assert client_factory.call_count == 1
        allow_close.set()
        assert await reload_task is True
        assert client_factory.call_count == 2
        assert await hass.config_entries.async_unload(entry.entry_id)
