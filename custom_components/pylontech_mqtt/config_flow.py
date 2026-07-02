"""Config flow for the Pylontech MQTT integration."""

import asyncio
import time

import paho.mqtt.client as mqtt
import voluptuous as vol
from paho.mqtt.enums import CallbackAPIVersion

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


def _test_mqtt_connection(
    host: str, port: int, user: str = "", password: str = ""
) -> str | None:
    """Attempt a real MQTT connection; return None on success or an error key on failure.

    Returns ``"invalid_auth"`` when the broker rejects the credentials, or
    ``"cannot_connect"`` for all other failures (unreachable host, timeout, …).
    """
    outcome: list[str | None] = [None]

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            rc = getattr(reason_code, "value", None)
            outcome[0] = "invalid_auth" if rc in (4, 5) else "cannot_connect"
        else:
            outcome[0] = "ok"
        c.disconnect()

    client = mqtt.Client(CallbackAPIVersion.VERSION2)
    if user:
        client.username_pw_set(user, password)
    client.on_connect = on_connect

    try:
        client.connect(host, port, keepalive=10)
    except OSError:
        return "cannot_connect"

    deadline = time.monotonic() + 5.0
    while outcome[0] is None and time.monotonic() < deadline:
        client.loop(timeout=0.2)

    try:
        client.disconnect()
        client.loop(timeout=0.2)
    except Exception:
        pass

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
            vol.Required(CONF_MQTT_PORT, default=default_port): vol.All(int, vol.Range(min=1, max=65535)),
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
                new_unique_id = f"{host}:{port}:{user_input[CONF_MQTT_TOPIC]}"
                for other in self.hass.config_entries.async_entries(DOMAIN):
                    if (
                        other.entry_id != self.config_entry.entry_id
                        and other.unique_id == new_unique_id
                    ):
                        errors["base"] = "already_configured"
                        break
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
