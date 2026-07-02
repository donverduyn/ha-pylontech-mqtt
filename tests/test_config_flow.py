"""Tests for Pylontech MQTT config flow (user setup and options update)."""
import asyncio
import pytest
from unittest.mock import patch
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

DOMAIN = "pylontech_mqtt"

_VALID_INPUT = {
    "mqtt_host": "localhost",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "mqtt_topic": "pylontech/stack",
}

# Shared patch helpers
_PATCH_CONN = "custom_components.pylontech_mqtt.config_flow._test_mqtt_connection"
_PATCH_SETUP = "custom_components.pylontech_mqtt.coordinator.PylontechCoordinator.setup"


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations) -> None:
    """Enable custom integration discovery for every test in this file."""


# ---------------------------------------------------------------------------
# Initial config flow (user step)
# ---------------------------------------------------------------------------

async def test_form_shown_on_user_step(hass: HomeAssistant) -> None:
    """Opening the config flow must show the MQTT broker form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_cannot_connect_shows_error(hass: HomeAssistant) -> None:
    """A broker that is unreachable must produce a cannot_connect base error."""
    with patch(_PATCH_CONN, return_value="cannot_connect"):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], _VALID_INPUT
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_invalid_auth_shows_error(hass: HomeAssistant) -> None:
    """Wrong credentials must produce an invalid_auth base error."""
    with patch(_PATCH_CONN, return_value="invalid_auth"):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], _VALID_INPUT
        )
    assert result["errors"]["base"] == "invalid_auth"


async def test_successful_entry_created(hass: HomeAssistant) -> None:
    """Valid credentials and reachable broker must create a config entry."""
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], _VALID_INPUT
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
        result2 = await hass.config_entries.flow.async_configure(
            init2["flow_id"], _VALID_INPUT
        )
    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


async def test_timeout_treated_as_cannot_connect(hass: HomeAssistant) -> None:
    """If broker validation times out, the flow must show cannot_connect."""
    with patch(
        "custom_components.pylontech_mqtt.config_flow.asyncio.wait_for",
        side_effect=asyncio.TimeoutError,
    ):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            init["flow_id"], _VALID_INPUT
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
    result = await hass.config_entries.options.async_init(entry.entry_id)
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
        result = await hass.config_entries.options.async_configure(
            opts["flow_id"],
            {**_VALID_INPUT, "mqtt_host": "192.168.1.99"},
        )
    assert result["errors"]["base"] == "cannot_connect"


async def test_options_updated_successfully(hass: HomeAssistant) -> None:
    """Valid options update must produce a CREATE_ENTRY result."""
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init["flow_id"], _VALID_INPUT)

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    opts = await hass.config_entries.options.async_init(entry.entry_id)

    new_input = {**_VALID_INPUT, "mqtt_host": "192.168.1.5"}
    with patch(_PATCH_CONN, return_value=None), patch(_PATCH_SETUP):
        result = await hass.config_entries.options.async_configure(
            opts["flow_id"], new_input
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["mqtt_host"] == "192.168.1.5"
