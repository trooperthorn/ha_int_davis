import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .client import DavisVantageClient
from .const import (
    CONFIG_BAUD_RATE,
    CONFIG_INTERVAL,
    CONFIG_LINK,
    CONFIG_MINIMAL_INTERVAL,
    CONFIG_PERSISTENT_CONNECTION,
    CONFIG_PROTOCOL,
    DEFAULT_BAUD_RATE,
    DEFAULT_SYNC_INTERVAL,
    DOMAIN,
    NAME,
    PROTOCOL_NETWORK,
    PROTOCOL_SERIAL,
    SUPPORTED_BAUD_RATES,
)

_LOGGER = logging.getLogger(__name__)

# Schema used for reconfiguring an existing integration entry
RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONFIG_LINK): str,
        # LOOP 2 enable when working
#        vol.Optional("use_loop2", default=False): bool,
        vol.Required(CONFIG_INTERVAL, default=DEFAULT_SYNC_INTERVAL): vol.All(
            int, vol.Range(min=CONFIG_MINIMAL_INTERVAL)  # type: ignore
        ),
    }
)

class PlaceholderHub:
    """Test connection to the Davis Weather Station."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        self._hass = hass

    async def authenticate(self, protocol: str, link: str) -> bool:
        """Test if we can connect to the station via the provided link."""
        client = DavisVantageClient(self._hass, protocol, link, False)
        try:
            await client.connect_to_station()
            davis_time = await client.async_get_davis_time()
            return davis_time is not None
        except Exception as err:
            # Logs raw error output ONLY on failure
            _LOGGER.error(
                "Failed to connect to Davis station on %s://%s. Raw error: %s",
                protocol,
                link,
                repr(err),
            )
            return False


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    hub = PlaceholderHub(hass)
    if not await hub.authenticate(data[CONFIG_PROTOCOL], data[CONFIG_LINK]):
        raise CannotConnect

    return {"title": f"Davis Vantage ({data[CONFIG_LINK]})"}


class DavisVantageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Davis Vantage."""

    VERSION = 1
    protocol: str
    link: str

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial protocol selection step."""
        if user_input is not None:
            self.protocol = user_input[CONFIG_PROTOCOL]
            if self.protocol == PROTOCOL_SERIAL:
                return await self.async_step_setup_serial()

            return await self.async_step_setup_network()

        list_of_types = [PROTOCOL_SERIAL, PROTOCOL_NETWORK]
        schema = vol.Schema({vol.Required(CONFIG_PROTOCOL): vol.In(list_of_types)})
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_setup_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle selecting a serial USB port."""
        errors = {}

        if user_input is not None:
            self.link = user_input[CONFIG_LINK]

            # --- FAST PROBE FOR INSTANT FEEDBACK, BAUD NEGOTIATION & LOOP 2 DISCOVERY ---
            def wake_console(ser) -> bool:
                import time
                ser.write(b"\n")
                time.sleep(0.1)
                if ser.read(2) == b"\n\r":
                    return True
                # Davis consoles sometimes need a second wake-up call if deeply asleep
                ser.write(b"\n")
                time.sleep(0.1)
                return ser.read(2) == b"\n\r"

            def fast_probe_davis(port: str) -> tuple[bool, bool, int]:
                """Probe the port, negotiating the console's current baud rate.

                Tries the default (fastest) baud first, then falls back through
                the other console-supported rates so setup works whether or not
                the console has previously been reconfigured to a slower rate.
                """
                import serial
                import time

                for baud_rate in SUPPORTED_BAUD_RATES:
                    try:
                        with serial.Serial(port, baud_rate, timeout=2) as ser:
                            if not wake_console(ser):
                                continue

                            # Test LOOP 2 support instantly
                            ser.write(b"LPS 2 1\n")
                            time.sleep(0.1)
                            ack = ser.read(1)
                            loop2_supported = ack == b"\x06"

                            return True, loop2_supported, baud_rate
                    except Exception:
                        continue

                return False, False, DEFAULT_BAUD_RATE

            # Run the fast probe without blocking Home Assistant
            success, loop2_supported, baud_rate = await self.hass.async_add_executor_job(
                fast_probe_davis, self.link
            )

            if success:
                # Save the discovered capabilities to the class instance
                self.loop2_supported = loop2_supported
                self.baud_rate = baud_rate
                return await self.async_step_setup_other_info()
            else:
                # Instantly throw the specific error!
                _LOGGER.warning("Connection test failed on %s: BAD ACK.", self.link)
                errors["base"] = "no_davis_device"
            # ---------------------------------------------------------

        step_user_data_schema = vol.Schema(
            {vol.Required(CONFIG_LINK): selector.SerialPortSelector()}
        )

        return self.async_show_form(
            step_id="setup_serial", data_schema=step_user_data_schema, errors=errors
        )

    async def async_step_setup_network(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle network connection details."""
        if user_input is not None:
            self.link = user_input[CONFIG_LINK]
            return await self.async_step_setup_other_info()

        step_user_data_schema = vol.Schema({vol.Required(CONFIG_LINK): str})

        return self.async_show_form(
            step_id="setup_network", data_schema=step_user_data_schema
        )

    async def async_step_setup_other_info(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle additional settings after successful connection."""
        if user_input is not None:
            user_input[CONFIG_LINK] = getattr(self, "link", "")
            user_input[CONFIG_PROTOCOL] = "Serial"
            user_input[CONFIG_BAUD_RATE] = getattr(self, "baud_rate", DEFAULT_BAUD_RATE)
            return self.async_create_entry(title="Davis Vantage", data=user_input)

        auto_loop2 = getattr(self, "loop2_supported", False)

        step_user_data_schema = vol.Schema(
            {
                # Set minimum to 30 seconds and maximum to 300 seconds
                vol.Required(CONFIG_INTERVAL, default=300): vol.All(
                    int, vol.Range(min=30, max=1800)
                ),
                vol.Optional("use_loop2", default=auto_loop2): bool,
                vol.Optional(CONFIG_PERSISTENT_CONNECTION, default=False): bool,
            }
        )

        return self.async_show_form(
            step_id="setup_other_info", data_schema=step_user_data_schema
        )

    async def async_step_reconfigure(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a reconfiguration flow initialized by the user."""
        self.entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reconfigure_confirm()

    async def async_step_reconfigure_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reconfiguration changes."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                self.entry, data=self.entry.data | user_input  # type: ignore
            )
            await self.hass.config_entries.async_reload(self.entry.entry_id)  # type: ignore
            return self.async_abort(reason="reconfigure_successful")

        step_user_data_schema = RECONFIGURE_SCHEMA
        if self.entry.data.get(CONFIG_PROTOCOL) == PROTOCOL_SERIAL:  # type: ignore
            step_user_data_schema = RECONFIGURE_SCHEMA.extend(
                {vol.Required(CONFIG_LINK): selector.SerialPortSelector()},
                required=True,
            )

        return self.async_show_form(
            step_id="reconfigure_confirm",
            data_schema=self.add_suggested_values_to_schema(
                data_schema=step_user_data_schema,
                suggested_values=self.entry.data | (user_input or {}),  # type: ignore
            ),
            description_placeholders={"name": self.entry.title},  # type: ignore
            errors=errors,
        )
    
    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Tell Home Assistant to use our Options Flow handler."""
        return DavisVantageOptionsFlowHandler()


class DavisVantageOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Options Flow for Davis Vantage."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # When the user clicks Submit, save the new options
            return self.async_create_entry(title="", data=user_input)

        # Grab the current settings to populate the form defaults. 
        # (self.config_entry still works below because HA injects it dynamically!)
        current_loop2 = self.config_entry.options.get(
            "use_loop2", self.config_entry.data.get("use_loop2", False)
        )
        current_interval = self.config_entry.options.get(
            CONFIG_INTERVAL, self.config_entry.data.get(CONFIG_INTERVAL, DEFAULT_SYNC_INTERVAL)
        )

        # Build the Configure form
        options_schema = vol.Schema(
            {
                vol.Optional("use_loop2", default=current_loop2): bool,
                vol.Required(CONFIG_INTERVAL, default=current_interval): vol.All(
                    int, vol.Range(min=CONFIG_MINIMAL_INTERVAL)  # type: ignore
                ),
            }
        )

        return self.async_show_form(
            step_id="init", data_schema=options_schema
        )

class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate invalid authentication."""
