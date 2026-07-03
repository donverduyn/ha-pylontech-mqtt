"""Config flow for the Pylontech MQTT integration."""

import asyncio
import logging
import time

import paho.mqtt.client as mqtt
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from paho.mqtt.enums import CallbackAPIVersion

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

_LOGGER = logging.getLogger(__name__)


def _reason_code_to_error(reason_code) -> str | None:
    """Map a CONNACK reason code to a config-flow error key, or None on success."""
    if not reason_code.is_failure:
        return None
    rc = getattr(reason_code, "value", None)
    return "invalid_auth" if rc in (4, 5) else "cannot_connect"


def _test_mqtt_connection(
    host: str, port: int, user: str = "", password: str = ""
) -> str | None:
    """Attempt a real MQTT connection; return None on success or an error key on failure.

    Returns ``"invalid_auth"`` when the broker rejects the credentials, or
    ``"cannot_connect"`` for all other failures (unreachable host, timeout, …).
    """
    outcome: list[str | None] = [None]

    def on_connect(c, userdata, flags, reason_code, properties):
        outcome[0] = _reason_code_to_error(reason_code) or "ok"
        c.disconnect()

    client = mqtt.Client(CallbackAPIVersion.VERSION2)
    if user:
        client.username_pw_set(user, password)
    client.on_connect = on_connect

    try:
        client.connect(host, port, keepalive=10)
    except (OSError, ValueError):
        # OSError  — host unreachable, connection refused, etc.
        # ValueError — paho raises this for an empty or syntactically invalid
        #              hostname before any network I/O is attempted.
        return "cannot_connect"

    deadline = time.monotonic() + 5.0
    while outcome[0] is None and time.monotonic() < deadline:
        client.loop(timeout=0.2)

    try:
        client.disconnect()
        client.loop(timeout=0.2)
    except Exception as err:
        _LOGGER.debug("Ignoring error during MQTT client cleanup: %s", err)

    return None if outcome[0] == "ok" else (outcome[0] or "cannot_connect")


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
            vol.Required(CONF_MQTT_PORT, default=default_port): vol.All(
                int, vol.Range(min=1, max=65535)
            ),
            vol.Optional(CONF_MQTT_USER, default=default_user): str,
            vol.Optional(CONF_MQTT_PASS, default=default_pass): str,
            vol.Required(CONF_MQTT_TOPIC, default=default_topic): str,
        }
    )


def _mqtt_unique_id(host: str, port: int, topic: str) -> str:
    return f"{host}:{port}:{topic}"


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pylontech MQTT."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Single-step flow: MQTT broker settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_MQTT_HOST]
            port = user_input[CONF_MQTT_PORT]

            try:
                conn_error = await asyncio.wait_for(
                    self.hass.async_add_executor_job(
                        _test_mqtt_connection,
                        host,
                        port,
                        user_input.get(CONF_MQTT_USER, ""),
                        user_input.get(CONF_MQTT_PASS, ""),
                    ),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                conn_error = "cannot_connect"
            if conn_error:
                errors["base"] = conn_error
            else:
                await self.async_set_unique_id(
                    _mqtt_unique_id(host, port, user_input[CONF_MQTT_TOPIC])
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

            try:
                conn_error = await asyncio.wait_for(
                    self.hass.async_add_executor_job(
                        _test_mqtt_connection,
                        host,
                        port,
                        user_input.get(CONF_MQTT_USER, ""),
                        user_input.get(CONF_MQTT_PASS, ""),
                    ),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                conn_error = "cannot_connect"
            if conn_error:
                errors["base"] = conn_error
            else:
                new_unique_id = _mqtt_unique_id(
                    host, port, user_input[CONF_MQTT_TOPIC]
                )
                for other in self.hass.config_entries.async_entries(DOMAIN):
                    other_host = other.options.get(
                        CONF_MQTT_HOST, other.data.get(CONF_MQTT_HOST, "")
                    )
                    other_port = other.options.get(
                        CONF_MQTT_PORT, other.data.get(CONF_MQTT_PORT, DEFAULT_MQTT_PORT)
                    )
                    other_topic = other.options.get(
                        CONF_MQTT_TOPIC,
                        other.data.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC),
                    )
                    other_effective_unique_id = _mqtt_unique_id(
                        other_host, other_port, other_topic
                    )
                    if (
                        other.entry_id != self.config_entry.entry_id
                        and (
                            other.unique_id == new_unique_id
                            or other_effective_unique_id == new_unique_id
                        )
                    ):
                        errors["base"] = "already_configured"
                        break
                else:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, unique_id=new_unique_id
                    )
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
