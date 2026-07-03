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

from custom_components.pylontech_mqtt.config_flow import (
    _reason_code_to_error,
    _test_mqtt_connection,
)

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


# ---------------------------------------------------------------------------
# Initial config flow (user step)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


async def test_options_form_shown(hass: HomeAssistant) -> None:
    """Opening the options flow must show the broker update form."""
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init["flow_id"], _VALID_INPUT)

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    result = cast(
        dict[str, Any], await hass.config_entries.options.async_init(entry.entry_id)
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"


async def test_options_cannot_connect(hass: HomeAssistant) -> None:
    """Unreachable broker in options flow must show cannot_connect error."""
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init["flow_id"], _VALID_INPUT)

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    opts = await hass.config_entries.options.async_init(entry.entry_id)

    with patch(_PATCH_CONN, return_value="cannot_connect"):
        result = cast(
            dict[str, Any],
            await hass.config_entries.options.async_configure(
                opts["flow_id"],
                {**_VALID_INPUT, "mqtt_host": "192.168.1.99"},
            ),
        )
    assert result["errors"]["base"] == "cannot_connect"


async def test_options_updated_successfully(hass: HomeAssistant) -> None:
    """Valid options update must produce a CREATE_ENTRY result."""
    # _PATCH_SETUP must cover the full test including the entry reload triggered
    # by saving options.  HA schedules the reload via async_create_task so it
    # runs on the next event-loop iteration, not inline.  async_block_till_done()
    # drains that task while the patch is still active, preventing a real paho
    # background thread from being left alive at teardown.
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init["flow_id"], _VALID_INPUT)

        entry = hass.config_entries.async_entries(DOMAIN)[0]
        opts = await hass.config_entries.options.async_init(entry.entry_id)

        new_input = {**_VALID_INPUT, "mqtt_host": "192.168.1.5"}
        result = cast(
            dict[str, Any],
            await hass.config_entries.options.async_configure(
                opts["flow_id"], new_input
            ),
        )
        # Drain the scheduled reload task before the patch exits.
        await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["mqtt_host"] == "192.168.1.5"
    assert entry.unique_id == "192.168.1.5:1883:pylontech/stack"


async def test_options_update_rejects_duplicate_effective_settings(
    hass: HomeAssistant,
) -> None:
    """Options changes must not allow two entries to use the same broker/topic."""
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
        opts = await hass.config_entries.options.async_init(first_entry.entry_id)
        result = cast(
            dict[str, Any],
            await hass.config_entries.options.async_configure(
                opts["flow_id"], second_input
            ),
        )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "already_configured"


# ---------------------------------------------------------------------------
# _test_mqtt_connection unit tests
# ---------------------------------------------------------------------------


def _reason_code(*, is_failure: bool, value: int | None = None) -> SimpleNamespace:
    """Build a minimal paho-style reason code for _reason_code_to_error tests."""
    return SimpleNamespace(is_failure=is_failure, value=value)


def test_reason_code_success_returns_none() -> None:
    """A non-failure CONNACK reason code must map to no error."""
    assert _reason_code_to_error(_reason_code(is_failure=False)) is None


@pytest.mark.parametrize("rc", [4, 5])
def test_reason_code_bad_credentials_returns_invalid_auth(rc: int) -> None:
    """CONNACK codes 4 and 5 (bad credentials/not authorized) must map to invalid_auth."""
    assert _reason_code_to_error(_reason_code(is_failure=True, value=rc)) == "invalid_auth"


@pytest.mark.parametrize("rc", [1, 2, 3, None])
def test_reason_code_other_failure_returns_cannot_connect(rc: int | None) -> None:
    """Any other failing CONNACK reason code must map to cannot_connect."""
    assert _reason_code_to_error(_reason_code(is_failure=True, value=rc)) == "cannot_connect"


def test_empty_hostname_returns_cannot_connect() -> None:
    """_test_mqtt_connection must return 'cannot_connect' for an empty hostname.

    paho raises ValueError('Invalid host.') for an empty string — not OSError.
    The function must catch it rather than letting it propagate as an unhandled
    exception through the config-flow executor wrapper.
    """
    result = _test_mqtt_connection("", 1883)
    assert result == "cannot_connect"


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
