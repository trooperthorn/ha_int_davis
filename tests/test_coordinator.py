"""Integration tests for DavisVantageDataUpdateCoordinator's failure/repair-issue path.

Unlike test_client.py/test_utils.py/test_sensor.py (pure logic, no hass
needed), these exercise the coordinator against a real Home Assistant
instance via pytest-homeassistant-custom-component, since the behavior
under test - UpdateFailed propagation and issue_registry state - only
means something in that context.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.davis_vantage.const import DOMAIN
from custom_components.davis_vantage.coordinator import (
    CONNECTION_ISSUE_ID,
    CONNECTION_ISSUE_THRESHOLD,
    DavisVantageDataUpdateCoordinator,
)

pytestmark = pytest.mark.asyncio


def make_coordinator(hass, client):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, title="Davis Vantage (test)")
    entry.add_to_hass(hass)
    coordinator = DavisVantageDataUpdateCoordinator(
        hass=hass, client=client, device_info=MagicMock(), config_entry=entry
    )
    return coordinator, entry


def make_client(**get_current_data_kwargs) -> MagicMock:
    client = MagicMock()
    client.async_get_current_data = AsyncMock(**get_current_data_kwargs)
    return client


async def test_successful_update_returns_data_and_resets_streak(hass):
    client = make_client(return_value={"LastError": "", "TempOut": 72})
    coordinator, _ = make_coordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data["TempOut"] == 72
    assert coordinator._consecutive_failures == 0


async def test_last_error_field_raises_update_failed(hass):
    # async_get_current_data() catches its own errors and always returns a
    # dict - a set LastError must still surface as a coordinator failure,
    # not a silent "successful" update with stale data.
    client = make_client(return_value={"LastError": "console did not ACK"})
    coordinator, _ = make_coordinator(hass, client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator._consecutive_failures == 1


async def test_client_timeout_raises_update_failed(hass):
    client = make_client(side_effect=TimeoutError())
    coordinator, _ = make_coordinator(hass, client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator._consecutive_failures == 1


async def test_unexpected_exception_raises_update_failed(hass):
    client = make_client(side_effect=RuntimeError("serial port vanished"))
    coordinator, _ = make_coordinator(hass, client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator._consecutive_failures == 1


async def test_single_failure_does_not_create_a_repair_issue(hass):
    client = make_client(return_value={"LastError": "boom"})
    coordinator, _ = make_coordinator(hass, client)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert ir.async_get(hass).async_get_issue(DOMAIN, CONNECTION_ISSUE_ID) is None


async def test_repair_issue_created_after_sustained_failure(hass):
    client = make_client(return_value={"LastError": "boom"})
    coordinator, _ = make_coordinator(hass, client)

    for _ in range(CONNECTION_ISSUE_THRESHOLD):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, CONNECTION_ISSUE_ID)
    assert issue is not None
    assert issue.translation_key == "connection_lost"


async def test_repair_issue_cleared_on_recovery(hass):
    client = make_client(return_value={"LastError": "boom"})
    coordinator, _ = make_coordinator(hass, client)

    for _ in range(CONNECTION_ISSUE_THRESHOLD):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
    assert ir.async_get(hass).async_get_issue(DOMAIN, CONNECTION_ISSUE_ID) is not None

    client.async_get_current_data = AsyncMock(return_value={"LastError": "", "TempOut": 70})
    data = await coordinator._async_update_data()

    assert data["TempOut"] == 70
    assert ir.async_get(hass).async_get_issue(DOMAIN, CONNECTION_ISSUE_ID) is None
    assert coordinator._consecutive_failures == 0
