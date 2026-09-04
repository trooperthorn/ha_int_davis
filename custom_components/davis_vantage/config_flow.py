"""Config and options flows for Davis Vantage."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SerialPortSelector,
    TextSelector,
)

from .const import (
    CONF_USE_LOOP2,
    CONFIG_BAUD_RATE,
    CONFIG_IDENTITY,
    CONFIG_IDENTITY_SOURCE,
    CONFIG_IDENTITY_STRENGTH,
    CONFIG_INTERVAL,
    CONFIG_LINK,
    CONFIG_LOOP2_SUPPORTED,
    CONFIG_MINIMAL_INTERVAL,
    CONFIG_PERSISTENT_CONNECTION,
    CONFIG_PROTOCOL,
    DEFAULT_SYNC_INTERVAL,
    DOMAIN,
    IDENTITY_STRONG,
    PROTOCOL_NETWORK,
    PROTOCOL_SERIAL,
)
from .verification import (
    DavisCannotConnectError,
    DavisNotFoundError,
    VerificationResult,
    verify_connection,
)

_LOGGER = logging.getLogger(__name__)

CONNECTION_SCHEMA = vol.Schema(
    {
        vol.Required(CONFIG_PROTOCOL): SelectSelector(
            SelectSelectorConfig(options=[PROTOCOL_SERIAL, PROTOCOL_NETWORK])
        )
    }
)


def _options_schema(
    *, interval: int, use_loop2: bool, persistent_connection: bool
) -> vol.Schema:
    """Build the single owner for runtime-tuning settings."""
    return vol.Schema(
        {
            vol.Required(CONFIG_INTERVAL, default=interval): vol.All(
                int, vol.Range(min=CONFIG_MINIMAL_INTERVAL, max=1800)
            ),
            vol.Optional(CONF_USE_LOOP2, default=use_loop2): bool,
            vol.Optional(
                CONFIG_PERSISTENT_CONNECTION, default=persistent_connection
            ): bool,
        }
    )


class DavisVantageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Davis Vantage."""

    VERSION = 2

    protocol: str
    link: str
    verification: VerificationResult | None = None
    _is_reconfigure = False
    _reconfigure_started = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose Serial/USB or WeatherLink network transport."""
        if user_input is not None:
            self.protocol = user_input[CONFIG_PROTOCOL]
            return await self.async_step_interface()
        return self.async_show_form(step_id="user", data_schema=CONNECTION_SCHEMA)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start a transport-aware reconfigure flow."""
        self._is_reconfigure = True
        entry = self._get_reconfigure_entry()
        if self._reconfigure_started and user_input is not None:
            self.protocol = user_input[CONFIG_PROTOCOL]
            return await self.async_step_interface()
        self._reconfigure_started = True
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                CONNECTION_SCHEMA,
                {CONFIG_PROTOCOL: entry.data.get(CONFIG_PROTOCOL, PROTOCOL_SERIAL)},
            ),
        )

    async def async_step_interface(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one interface without opening it while the form is displayed."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self.link = user_input[CONFIG_LINK]
            self.verification = None
            return await self.async_step_verify()

        suggested_link = ""
        if self._is_reconfigure:
            suggested_link = str(
                self._get_reconfigure_entry().data.get(CONFIG_LINK, "")
            )
        field = (
            SerialPortSelector()
            if self.protocol == PROTOCOL_SERIAL
            else TextSelector()
        )
        schema = vol.Schema({vol.Required(CONFIG_LINK): field})
        return self.async_show_form(
            step_id="interface",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONFIG_LINK: suggested_link}
            ),
            errors=errors,
        )

    async def async_step_verify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Verify only the submitted interface and show the detected capabilities."""
        if self.verification is None:
            try:
                self.verification = await self.hass.async_add_executor_job(
                    verify_connection, self.protocol, self.link
                )
            except DavisNotFoundError:
                return self.async_show_form(
                    step_id="interface",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONFIG_LINK, default=self.link): (
                                SerialPortSelector()
                                if self.protocol == PROTOCOL_SERIAL
                                else TextSelector()
                            )
                        }
                    ),
                    errors={"base": "no_davis_device"},
                )
            except DavisCannotConnectError:
                return self.async_show_form(
                    step_id="interface",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONFIG_LINK, default=self.link): (
                                SerialPortSelector()
                                if self.protocol == PROTOCOL_SERIAL
                                else TextSelector()
                            )
                        }
                    ),
                    errors={"base": "cannot_connect"},
                )
            except Exception:
                _LOGGER.exception("Unexpected exception verifying Davis interface")
                return self.async_show_form(
                    step_id="interface",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONFIG_LINK, default=self.link): (
                                SerialPortSelector()
                                if self.protocol == PROTOCOL_SERIAL
                                else TextSelector()
                            )
                        }
                    ),
                    errors={"base": "unknown"},
                )

        if user_input is not None:
            if self._is_reconfigure:
                return await self._async_finish_reconfigure()
            return await self.async_step_options()

        return self.async_show_form(
            step_id="verify",
            data_schema=vol.Schema({}),
            description_placeholders={
                "endpoint": self.verification.endpoint,
                "baud": (
                    str(self.verification.baud_rate)
                    if self.verification.baud_rate is not None
                    else "not applicable"
                ),
                "loop2": "supported" if self.verification.loop2_supported else "not supported",
            },
        )

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect runtime options after successful verification."""
        assert self.verification is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_USE_LOOP2) and not self.verification.loop2_supported:
                errors["base"] = "unsupported_loop2"
            else:
                identity = self.verification.identity
                source = self.verification.identity_source
                strength = self.verification.identity_strength
                if strength != IDENTITY_STRONG:
                    identity = f"generated:{uuid4()}"
                    source = "generated"
                unique_id = f"davis:{identity}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                self._async_abort_entries_match(
                    {
                        CONFIG_PROTOCOL: self.verification.protocol,
                        CONFIG_LINK: self.verification.endpoint,
                    }
                )
                data = self._connection_data(identity, source, strength)
                return self.async_create_entry(
                    title=f"Davis Vantage ({self.verification.endpoint})",
                    data=data,
                    options=dict(user_input),
                )

        return self.async_show_form(
            step_id="options",
            data_schema=_options_schema(
                interval=DEFAULT_SYNC_INTERVAL,
                use_loop2=self.verification.loop2_supported,
                persistent_connection=False,
            ),
            errors=errors,
        )

    def _connection_data(
        self, identity: str, identity_source: str, identity_strength: str
    ) -> dict[str, Any]:
        """Build config-entry data without duplicating options."""
        assert self.verification is not None
        data: dict[str, Any] = {
            CONFIG_PROTOCOL: self.verification.protocol,
            CONFIG_LINK: self.verification.endpoint,
            CONFIG_IDENTITY: identity,
            CONFIG_IDENTITY_SOURCE: identity_source,
            CONFIG_IDENTITY_STRENGTH: identity_strength,
            CONFIG_LOOP2_SUPPORTED: self.verification.loop2_supported,
        }
        if self.verification.baud_rate is not None:
            data[CONFIG_BAUD_RATE] = self.verification.baud_rate
        return data

    async def _async_finish_reconfigure(self) -> ConfigFlowResult:
        """Validate identity, update once, and let the update listener reload."""
        assert self.verification is not None
        entry = self._get_reconfigure_entry()
        old_strength = entry.data.get(CONFIG_IDENTITY_STRENGTH)
        new_strength = self.verification.identity_strength
        identity = entry.data.get(CONFIG_IDENTITY, self.verification.identity)
        source = entry.data.get(CONFIG_IDENTITY_SOURCE, "generated")
        unique_id = entry.unique_id

        if old_strength == IDENTITY_STRONG:
            if new_strength != IDENTITY_STRONG:
                return self.async_abort(reason="identity_unavailable")
            new_unique_id = f"davis:{self.verification.identity}"
            await self.async_set_unique_id(new_unique_id)
            self._abort_if_unique_id_mismatch(reason="wrong_device")
            identity = self.verification.identity
            source = self.verification.identity_source
            unique_id = new_unique_id
        elif new_strength == IDENTITY_STRONG:
            new_unique_id = f"davis:{self.verification.identity}"
            for other in self._async_current_entries():
                if other.entry_id != entry.entry_id and other.unique_id == new_unique_id:
                    raise AbortFlow("already_configured")
            identity = self.verification.identity
            source = self.verification.identity_source
            unique_id = new_unique_id

        for other in self._async_current_entries():
            if (
                other.entry_id != entry.entry_id
                and other.data.get(CONFIG_PROTOCOL) == self.verification.protocol
                and other.data.get(CONFIG_LINK) == self.verification.endpoint
            ):
                raise AbortFlow("already_configured")

        preserved = {
            key: value
            for key, value in entry.data.items()
            if key
            not in {
                CONFIG_PROTOCOL,
                CONFIG_LINK,
                CONFIG_BAUD_RATE,
                CONFIG_IDENTITY,
                CONFIG_IDENTITY_SOURCE,
                CONFIG_IDENTITY_STRENGTH,
                CONFIG_LOOP2_SUPPORTED,
                CONFIG_INTERVAL,
                CONF_USE_LOOP2,
                CONFIG_PERSISTENT_CONNECTION,
            }
        }
        data = preserved | self._connection_data(identity, source, new_strength)
        return self.async_update_and_abort(
            entry,
            unique_id=unique_id,
            title=f"Davis Vantage ({self.verification.endpoint})",
            data=data,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the Davis options flow."""
        return DavisVantageOptionsFlowHandler()


class DavisVantageOptionsFlowHandler(config_entries.OptionsFlow):
    """Manage the three runtime-owned options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        errors: dict[str, str] = {}
        loop2_supported = bool(
            self.config_entry.data.get(CONFIG_LOOP2_SUPPORTED, False)
        )
        if user_input is not None:
            if user_input.get(CONF_USE_LOOP2) and not loop2_supported:
                errors["base"] = "unsupported_loop2"
            else:
                return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(
                interval=options.get(CONFIG_INTERVAL, DEFAULT_SYNC_INTERVAL),
                use_loop2=options.get(CONF_USE_LOOP2, False),
                persistent_connection=options.get(
                    CONFIG_PERSISTENT_CONNECTION, False
                ),
            ),
            errors=errors,
        )
