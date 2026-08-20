"""Shared pytest fixtures for davis_vantage tests.

Today's suite covers the pure data-transform logic in client.py and
utils.py (parsing, unit correction, dash-value masking, LOOP1/LOOP2
handling) - none of it needs a running Home Assistant instance, only the
`homeassistant` package installed so the integration's modules import
cleanly (see requirements_test.txt).

Extending this to coordinator/config_flow tests (mocked serial I/O, a real
config entry lifecycle) should use `pytest-homeassistant-custom-component`,
which provides the `hass` fixture and friends - add it to
requirements_test.txt and enable custom integrations per its docs:

    import pytest
    pytest_plugins = "pytest_homeassistant_custom_component"

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        yield
"""
