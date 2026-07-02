"""Config flow for the Pylontech MQTT integration."""

import socket

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_MQTT_HOST,
    CONF_MQTT_PASS,
    CONF_MQTT_PORT,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USER,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
)


def _test_broker_reachable(host: str, port: int) -> bool:
    """Return True when the MQTT broker TCP port is reachable."""
    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except OSError:
        return False


def _broker_schema(
    default_host: str = "",
    default_port: int = DEFAULT_MQTT_PORT,
    default_user: str = "",
    default_pass: str = "",
    default_topic: str = DEFAULT_MQTT_TOPIC,
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_MQTT_HOST, default=default_host): str,
            vol.Required(CONF_MQTT_PORT, default=default_port): int,
            vol.Optional(CONF_MQTT_USER, default=default_user): str,
            vol.Optional(CONF_MQTT_PASS, default=default_pass): str,
            vol.Required(CONF_MQTT_TOPIC, default=default_topic): str,
        }
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pylontech MQTT."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Single-step flow: MQTT broker settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_MQTT_HOST]
            port = user_input[CONF_MQTT_PORT]

            reachable = await self.hass.async_add_executor_job(
                _test_broker_reachable, host, port
            )
            if not reachable:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(
                    f"{host}:{port}:{user_input[CONF_MQTT_TOPIC]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Pylontech ({host})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_broker_schema(),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Allow the user to update MQTT broker settings."""

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_MQTT_HOST]
            port = user_input[CONF_MQTT_PORT]

            reachable = await self.hass.async_add_executor_job(
                _test_broker_reachable, host, port
            )
            if not reachable:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="", data=user_input)

        entry = self.config_entry
        return self.async_show_form(
            step_id="init",
            data_schema=_broker_schema(
                default_host=entry.options.get(
                    CONF_MQTT_HOST, entry.data.get(CONF_MQTT_HOST, "")
                ),
                default_port=entry.options.get(
                    CONF_MQTT_PORT, entry.data.get(CONF_MQTT_PORT, DEFAULT_MQTT_PORT)
                ),
                default_user=entry.options.get(
                    CONF_MQTT_USER, entry.data.get(CONF_MQTT_USER, "")
                ),
                default_pass=entry.options.get(
                    CONF_MQTT_PASS, entry.data.get(CONF_MQTT_PASS, "")
                ),
                default_topic=entry.options.get(
                    CONF_MQTT_TOPIC, entry.data.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC)
                ),
            ),
            errors=errors,
        )
