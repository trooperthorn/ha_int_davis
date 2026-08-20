"""Shared pytest fixtures for davis_vantage tests.

test_utils.py, test_client.py, and test_sensor.py cover pure data-transform
logic (parsing, unit correction, dash-value masking, LOOP1/LOOP2 handling)
and don't need a running Home Assistant instance - just the `homeassistant`
package installed so the integration's modules import cleanly.

test_coordinator.py goes further: it exercises DavisVantageDataUpdateCoordinator
against a real `hass` instance via pytest-homeassistant-custom-component,
since the behavior under test (UpdateFailed propagation, issue_registry
state) only means something in that context. This fixture setup is what
that requires - see requirements_test.txt for the extra dependency.
"""
import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
