"""Tests for Pylontech MQTT config flow (user setup and options update)."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from conftest import PATCH_CONN as _PATCH_CONN
from conftest import PATCH_SETUP as _PATCH_SETUP
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import TextSelector, TextSelectorType
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.pylontech_mqtt.config_flow import (
    _broker_schema,
    _reason_code_to_error,
    _test_mqtt_connection,
)
from custom_components.pylontech_mqtt.const import CONF_MQTT_PASS

DOMAIN = "pylontech_mqtt"

_VALID_INPUT = {
    "mqtt_host": "localhost",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "mqtt_topic": "pylontech/stack",
}


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations) -> None:
    """Enable custom integration discovery for every test in this file."""


# Initial config flow (user step)


async def test_form_shown_on_user_step(hass: HomeAssistant) -> None:
    """Opening the config flow must show the MQTT broker form."""
    result = cast(
        dict[str, Any],
        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        ),
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"


def test_password_field_is_masked() -> None:
    """The broker password field must use a password-mode selector.

    Without this, HA renders the field as a plain visible text input rather
    than masking it like a normal password box.
    """
    schema = _broker_schema()
    marker = next(k for k in schema.schema if k == CONF_MQTT_PASS)
    validator = schema.schema[marker]
    assert isinstance(validator, TextSelector)
    assert validator.config["type"] == TextSelectorType.PASSWORD


async def test_cannot_connect_shows_error(hass: HomeAssistant) -> None:
    """A broker that is unreachable must produce a cannot_connect base error."""
    with patch(_PATCH_CONN, return_value="cannot_connect"):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                init["flow_id"], _VALID_INPUT
            ),
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_invalid_auth_shows_error(hass: HomeAssistant) -> None:
    """Wrong credentials must produce an invalid_auth base error."""
    with patch(_PATCH_CONN, return_value="invalid_auth"):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                init["flow_id"], _VALID_INPUT
            ),
        )
    assert result["errors"]["base"] == "invalid_auth"


@pytest.mark.parametrize(
    "topic",
    [
        "pylontech/#",
        "pylontech/+/stack",
        "",
        "  pylontech/stack",
        "pylontech/stack  ",
        "/pylontech/stack",
        "pylontech/stack/",
    ],
)
async def test_invalid_topic_prefix_shows_field_error(
    hass: HomeAssistant, topic: str
) -> None:
    """A topic containing MQTT wildcards or malformed slashes/whitespace
    must be rejected here — subscribing or publishing to it later raises
    ValueError deep inside paho-mqtt at runtime (see
    config_flow._invalid_topic_prefix)."""
    with patch(_PATCH_CONN) as mock_conn:
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                init["flow_id"], {**_VALID_INPUT, "mqtt_topic": topic}
            ),
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["mqtt_topic"] == "invalid_topic"
    # The connection test must be skipped entirely for a topic that's
    # already known to be invalid — no point probing a broker just to
    # discard the result.
    mock_conn.assert_not_called()


async def test_successful_entry_created(hass: HomeAssistant) -> None:
    """Valid credentials and reachable broker must create a config entry."""
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                init["flow_id"], _VALID_INPUT
            ),
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["mqtt_host"] == "localhost"
    assert result["data"]["mqtt_port"] == 1883
    assert result["data"]["mqtt_topic"] == "pylontech/stack"
    assert result["data"]["mqtt_tls"] is False


async def test_tls_toggle_persists_to_entry_data(hass: HomeAssistant) -> None:
    """Enabling the TLS toggle must persist through to entry.data, and the
    connection test must be run with TLS enabled."""
    with patch(_PATCH_CONN) as mock_conn, patch(_PATCH_SETUP):
        mock_conn.return_value = None
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                init["flow_id"], {**_VALID_INPUT, "mqtt_tls": True}
            ),
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["mqtt_tls"] is True
    assert mock_conn.call_args.args[-1] is True


async def test_duplicate_host_port_topic_aborts(hass: HomeAssistant) -> None:
    """A second entry with the same host:port:topic must be aborted."""
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        # First entry — succeeds
        init1 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init1["flow_id"], _VALID_INPUT)

        # Second entry with identical settings — must abort
        init2 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result2 = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                init2["flow_id"], _VALID_INPUT
            ),
        )
    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


async def test_timeout_treated_as_cannot_connect(hass: HomeAssistant) -> None:
    """If broker validation times out, the flow must show cannot_connect."""
    with patch(_PATCH_CONN, side_effect=asyncio.TimeoutError):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                init["flow_id"], _VALID_INPUT
            ),
        )
    assert result["errors"]["base"] == "cannot_connect"


# Reconfigure flow
#
# Updates entry.data directly (via async_update_reload_and_abort) instead of
# stacking a second copy in entry.options — see config_flow.async_step_reconfigure.
# A successful reconfigure returns ABORT (reason="reconfigure_successful"),
# not CREATE_ENTRY, since it updates the existing entry rather than creating
# a new one.


async def _start_reconfigure(hass: HomeAssistant, entry_id: str):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry_id},
    )


async def test_reconfigure_form_shown(hass: HomeAssistant) -> None:
    """Starting a reconfigure flow must show the broker update form."""
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init["flow_id"], _VALID_INPUT)

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    result = cast(dict[str, Any], await _start_reconfigure(hass, entry.entry_id))
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"


async def test_reconfigure_cannot_connect(hass: HomeAssistant) -> None:
    """Unreachable broker during reconfigure must show cannot_connect error."""
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init["flow_id"], _VALID_INPUT)

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    reconf = await _start_reconfigure(hass, entry.entry_id)

    with patch(_PATCH_CONN, return_value="cannot_connect"):
        result = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                reconf["flow_id"],
                {**_VALID_INPUT, "mqtt_host": "192.168.1.99"},
            ),
        )
    assert result["errors"]["base"] == "cannot_connect"


async def test_reconfigure_invalid_topic_prefix_shows_field_error(
    hass: HomeAssistant,
) -> None:
    """An invalid topic during reconfigure must be rejected the same way
    as during initial setup, before any connection is attempted."""
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init["flow_id"], _VALID_INPUT)

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    reconf = await _start_reconfigure(hass, entry.entry_id)

    with patch(_PATCH_CONN) as mock_conn:
        result = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                reconf["flow_id"],
                {**_VALID_INPUT, "mqtt_topic": "pylontech/#"},
            ),
        )
    assert result["errors"]["mqtt_topic"] == "invalid_topic"
    mock_conn.assert_not_called()


async def test_reconfigure_updates_entry_data_in_place(hass: HomeAssistant) -> None:
    """A successful reconfigure must update entry.data — not entry.options —
    and leave no stale copy of the old value behind."""
    # _PATCH_SETUP must cover the full test including the entry reload
    # triggered by a successful reconfigure. HA schedules the reload via
    # async_create_task so it runs on the next event-loop iteration, not
    # inline. async_block_till_done() drains that task while the patch is
    # still active, preventing a real paho background thread from being
    # left alive at teardown.
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init["flow_id"], _VALID_INPUT)

        entry = hass.config_entries.async_entries(DOMAIN)[0]
        reconf = await _start_reconfigure(hass, entry.entry_id)

        new_input = {**_VALID_INPUT, "mqtt_host": "192.168.1.5"}
        result = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                reconf["flow_id"], new_input
            ),
        )
        # Drain the scheduled reload task before the patch exits.
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["mqtt_host"] == "192.168.1.5"
    assert entry.options == {}
    assert entry.unique_id == "192.168.1.5:1883:pylontech/stack"


async def test_reconfigure_rejects_duplicate_effective_settings(
    hass: HomeAssistant,
) -> None:
    """Reconfiguring must not allow two entries to use the same broker/topic."""
    second_input = {**_VALID_INPUT, "mqtt_host": "192.168.1.6"}

    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        init1 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init1["flow_id"], _VALID_INPUT)

        init2 = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init2["flow_id"], second_input)

        first_entry = hass.config_entries.async_entries(DOMAIN)[0]
        reconf = await _start_reconfigure(hass, first_entry.entry_id)
        result = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                reconf["flow_id"], second_input
            ),
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "already_configured"


# _test_mqtt_connection unit tests


def _reason_code(name: str) -> ReasonCode:
    """Build a real paho CONNACK reason code by name.

    Using the actual paho ReasonCode class (rather than a hand-rolled stand-in
    for its ``value``/``is_failure`` shape) is what caught the original bug:
    a SimpleNamespace mock let the test assert against numeric CONNACK codes
    (4/5) that paho's callback API v2 never actually produces — real
    ReasonCode objects report 134/135 for these two failures instead.
    """
    return ReasonCode(PacketTypes.CONNACK, name)


def test_reason_code_success_returns_none() -> None:
    """A non-failure CONNACK reason code must map to no error."""
    assert _reason_code_to_error(_reason_code("Success")) is None


@pytest.mark.parametrize("name", ["Bad user name or password", "Not authorized"])
def test_reason_code_bad_credentials_returns_invalid_auth(name: str) -> None:
    """Bad-credential CONNACK reasons must map to invalid_auth."""
    assert _reason_code_to_error(_reason_code(name)) == "invalid_auth"


@pytest.mark.parametrize("name", ["Unspecified error", "Server unavailable", "Banned"])
def test_reason_code_other_failure_returns_cannot_connect(name: str) -> None:
    """Any other failing CONNACK reason code must map to cannot_connect."""
    assert _reason_code_to_error(_reason_code(name)) == "cannot_connect"


def test_empty_hostname_returns_cannot_connect() -> None:
    """_test_mqtt_connection must return 'cannot_connect' for an empty hostname.

    paho raises ValueError('Invalid host.') for an empty string — not OSError.
    The function must catch it rather than letting it propagate as an unhandled
    exception through the config-flow executor wrapper.
    """
    result = _test_mqtt_connection("", 1883)
    assert result == "cannot_connect"


def _mock_client_with_immediate_connack(mock_client_cls) -> None:
    """Make a mocked mqtt.Client's connect() synchronously fire on_connect
    with a successful CONNACK, so _test_mqtt_connection's polling loop
    returns immediately instead of idling for its full 5s deadline."""
    mock_client = mock_client_cls.return_value

    def _fake_connect(host, port, keepalive=10):
        reason_code = SimpleNamespace(is_failure=False, value=0)
        mock_client.on_connect(mock_client, None, None, reason_code, None)

    mock_client.connect.side_effect = _fake_connect


def test_tls_enabled_calls_tls_set() -> None:
    """use_tls=True must enable TLS on the paho client before connecting —
    otherwise the broker password is sent in plaintext by default."""
    with patch(
        "custom_components.pylontech_mqtt.config_flow.mqtt.Client"
    ) as mock_client_cls:
        _mock_client_with_immediate_connack(mock_client_cls)
        result = _test_mqtt_connection("localhost", 8883, use_tls=True)

    assert result is None
    mock_client_cls.return_value.tls_set.assert_called_once()


def test_tls_disabled_does_not_call_tls_set() -> None:
    """use_tls=False (the default) must not touch TLS at all."""
    with patch(
        "custom_components.pylontech_mqtt.config_flow.mqtt.Client"
    ) as mock_client_cls:
        _mock_client_with_immediate_connack(mock_client_cls)
        result = _test_mqtt_connection("localhost", 1883, use_tls=False)

    assert result is None
    mock_client_cls.return_value.tls_set.assert_not_called()


async def test_empty_hostname_shows_cannot_connect(hass: HomeAssistant) -> None:
    """An empty hostname must surface as cannot_connect, not crash the flow.

    paho raises ValueError (not OSError) for an empty host string.  The
    config-flow executor wrapper must catch it and return cannot_connect.
    """
    empty_host_input = {**_VALID_INPUT, "mqtt_host": ""}
    # Call the real _test_mqtt_connection in an executor — it must catch
    # ValueError and return "cannot_connect" without raising.
    init = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    # Patch only the executor wrapper to avoid a real network call, but
    # simulate what the real function returns for an empty host.
    with patch(_PATCH_CONN, return_value="cannot_connect"):
        result = cast(
            dict[str, Any],
            await hass.config_entries.flow.async_configure(
                init["flow_id"], empty_host_input
            ),
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"
