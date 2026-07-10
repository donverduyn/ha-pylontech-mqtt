"""Config flow for the Pylontech MQTT integration."""

import asyncio
import logging
import time
from typing import Any

import paho.mqtt.client as mqtt
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant

# TextSelector's own declared type is partially unknown (a gap in HA core's
# stubs, not this integration) — only the import triggers it, not any use
# of the class below.
from homeassistant.helpers.selector import (
    TextSelector,  # pyright: ignore[reportUnknownVariableType]
    TextSelectorConfig,
    TextSelectorType,
)
from paho.mqtt.client import ConnectFlags
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from .const import (
    CONF_MQTT_HOST,
    CONF_MQTT_PASS,
    CONF_MQTT_PORT,
    CONF_MQTT_TLS,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USER,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


_AUTH_FAILURE_REASON_NAMES = ("Bad user name or password", "Not authorized")


def _reason_code_to_error(reason_code: ReasonCode) -> str | None:
    """Map a CONNACK reason code to a config-flow error key, or None on success.

    Compared by name rather than numeric value: paho's ReasonCode.value is
    134/135 for these two failures under the callback API v2 (even for a
    plain MQTTv3.1.1 connection, since paho internally maps the legacy 4/5
    CONNACK codes onto the MQTTv5 reason-code numbering before invoking the
    v2 callback) — matching against the historical MQTTv3.1.1 codes 4/5
    never fires, so every real auth failure was falling through to
    "cannot_connect". Comparing against the named string is correct
    regardless of which numbering the underlying protocol version uses.
    """
    if not reason_code.is_failure:
        return None
    return (
        "invalid_auth"
        if reason_code in _AUTH_FAILURE_REASON_NAMES
        else "cannot_connect"
    )


def _test_mqtt_connection(
    host: str, port: int, user: str = "", password: str = "", use_tls: bool = False
) -> str | None:
    """Attempt a real MQTT connection; return None on success or an error key.

    Returns ``"invalid_auth"`` when the broker rejects the credentials, or
    ``"cannot_connect"`` for all other failures (unreachable host, timeout,
    TLS handshake failure, …).
    """
    outcome: list[str | None] = [None]

    def on_connect(
        c: mqtt.Client,
        userdata: Any,
        flags: ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        outcome[0] = _reason_code_to_error(reason_code) or "ok"
        c.disconnect()

    client = mqtt.Client(CallbackAPIVersion.VERSION2)
    if user:
        client.username_pw_set(user, password)
    if use_tls:
        # paho-mqtt imports `ssl` only under `if TYPE_CHECKING:`, and pyright
        # can't resolve ssl.VerifyMode from there in this version, making
        # tls_set's own inferred type partially unknown regardless of the
        # (argument-less) call here.
        client.tls_set()  # pyright: ignore[reportUnknownMemberType]
    client.on_connect = on_connect

    try:
        client.connect(host, port, keepalive=10)
    except (OSError, ValueError):
        # OSError  — host unreachable, connection refused, TLS handshake
        #            failure (e.g. self-signed/untrusted cert), etc.
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
    default_tls: bool = False,
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_MQTT_HOST, default=default_host): str,
            vol.Required(CONF_MQTT_PORT, default=default_port): vol.All(
                int, vol.Range(min=1, max=65535)
            ),
            vol.Optional(CONF_MQTT_USER, default=default_user): str,
            vol.Optional(CONF_MQTT_PASS, default=default_pass): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_MQTT_TOPIC, default=default_topic): str,
            vol.Optional(CONF_MQTT_TLS, default=default_tls): bool,
        }
    )


def _mqtt_unique_id(host: str, port: int, topic: str) -> str:
    return f"{host}:{port}:{topic}"


def _invalid_topic_prefix(topic: str) -> bool:
    """Return True if *topic* would break MQTT subscribe/publish at runtime.

    "#" and "+" are MQTT wildcard characters — legal in a subscription
    *filter* but not in a literal topic. Both the sidecar and this
    integration publish/subscribe using topic_prefix + "/state" verbatim,
    so a prefix containing either raises ValueError deep inside paho-mqtt
    at runtime, well after this form has already accepted it. Leading/
    trailing slashes and whitespace are rejected too, since they produce a
    prefix that doesn't round-trip cleanly through "{prefix}/state".
    """
    return (
        not topic
        or topic != topic.strip()
        or "#" in topic
        or "+" in topic
        or topic.startswith("/")
        or topic.endswith("/")
    )


async def _validate_broker(
    hass: HomeAssistant, host: str, port: int, user_input: dict[str, Any]
) -> str | None:
    """Run _test_mqtt_connection in the executor with a timeout.

    Returns None on success, or an error key on failure/timeout.
    """
    try:
        return await asyncio.wait_for(
            hass.async_add_executor_job(
                _test_mqtt_connection,
                host,
                port,
                user_input.get(CONF_MQTT_USER, ""),
                user_input.get(CONF_MQTT_PASS, ""),
                user_input.get(CONF_MQTT_TLS, False),
            ),
            timeout=10.0,
        )
    except TimeoutError:
        return "cannot_connect"


async def _validate_topic_and_broker(
    hass: HomeAssistant, host: str, port: int, user_input: dict[str, Any]
) -> dict[str, str]:
    """Validate the topic prefix, then broker connectivity if that passes.

    Returns an empty dict on success, or a single-entry errors dict keyed the
    same way async_show_form expects (shared by async_step_user and
    async_step_reconfigure, which otherwise diverge in what they do next).
    """
    if _invalid_topic_prefix(user_input[CONF_MQTT_TOPIC]):
        return {"mqtt_topic": "invalid_topic"}
    conn_error = await _validate_broker(hass, host, port, user_input)
    if conn_error:
        return {"base": conn_error}
    return {}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pylontech MQTT."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Single-step flow: MQTT broker settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_MQTT_HOST]
            port = user_input[CONF_MQTT_PORT]

            errors = await _validate_topic_and_broker(self.hass, host, port, user_input)
            if not errors:
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user update broker settings for an existing entry.

        Updates entry.data in place (via async_update_reload_and_abort)
        instead of stacking changes in entry.options — broker credentials
        are setup data, not runtime-tunable options, and a single source of
        truth means an old password can't linger in storage after rotation.
        """
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_MQTT_HOST]
            port = user_input[CONF_MQTT_PORT]

            errors = await _validate_topic_and_broker(self.hass, host, port, user_input)
            if not errors:
                new_unique_id = _mqtt_unique_id(host, port, user_input[CONF_MQTT_TOPIC])
                for other in self.hass.config_entries.async_entries(DOMAIN):
                    if other.entry_id == reconfigure_entry.entry_id:
                        continue
                    other_unique_id = _mqtt_unique_id(
                        other.data.get(CONF_MQTT_HOST, ""),
                        other.data.get(CONF_MQTT_PORT, DEFAULT_MQTT_PORT),
                        other.data.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC),
                    )
                    if (
                        other.unique_id == new_unique_id
                        or other_unique_id == new_unique_id
                    ):
                        errors["base"] = "already_configured"
                        break
                else:
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        unique_id=new_unique_id,
                        # Merge onto the existing data rather than replacing
                        # it outright, so hidden non-schema keys (e.g. the
                        # registry-identity token __init__ stashes there)
                        # survive the update instead of being silently
                        # dropped and losing their migration trail.
                        data={**reconfigure_entry.data, **user_input},
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_broker_schema(
                default_host=reconfigure_entry.data.get(CONF_MQTT_HOST, ""),
                default_port=reconfigure_entry.data.get(
                    CONF_MQTT_PORT, DEFAULT_MQTT_PORT
                ),
                default_user=reconfigure_entry.data.get(CONF_MQTT_USER, ""),
                default_pass=reconfigure_entry.data.get(CONF_MQTT_PASS, ""),
                default_topic=reconfigure_entry.data.get(
                    CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC
                ),
                default_tls=reconfigure_entry.data.get(CONF_MQTT_TLS, False),
            ),
            errors=errors,
        )
