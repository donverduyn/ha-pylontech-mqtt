"""End-to-end integration tests: entry setup → MQTT payload → sensor states."""

from typing import Any
from unittest.mock import patch

import pytest
from conftest import PATCH_SETUP as _PATCH_SETUP
from conftest import create_config_entry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.pylontech_mqtt.const import DOMAIN

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_ENTRY_DATA: dict[str, Any] = {
    "mqtt_host": "localhost",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "mqtt_topic": "pylontech/stack",
}

_PAYLOAD: dict[str, Any] = {
    "voltage": 51.2,
    "current": 10.0,
    "soc": 80.0,
    "power": 512.0,
    "energy_in": 10.5,
    "energy_out": 5.2,
    "spec": "48V/100AH",
    "manufacturer": "Pylon",
    "model": "US5KBPL",
    "batteries": [
        {
            "sys_id": 1,
            "voltage": 51.2,
            "current": 10.0,
            "temperature": 25.0,
            "soc": 80,
            "status": "Charge",
            "power": 512.0,
            "raw": "",
            "cells": [
                {
                    "cell_id": 0,
                    "voltage": 3.4,
                    "current": 0.5,
                    "temperature": 25.0,
                    "base_state": "Charge",
                    "soc": 80,
                }
            ],
        }
    ],
}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations) -> None:
    """Enable custom integration discovery for every test in this file."""


async def _create_entry(hass: HomeAssistant):
    """Create a config entry via the UI flow; return (entry, coordinator)."""
    return await create_config_entry(hass, _ENTRY_DATA)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIntegration:
    async def test_system_sensors_registered_immediately_on_setup(
        self, hass: HomeAssistant
    ) -> None:
        """System-level sensors must be in the entity registry right after entry loads."""
        entry, _ = await _create_entry(hass)
        ent_reg = er.async_get(hass)
        assert (
            ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_voltage")
            is not None
        )

    async def test_no_battery_sensors_before_first_message(
        self, hass: HomeAssistant
    ) -> None:
        """Battery sensors must not exist before any MQTT payload arrives."""
        entry, _ = await _create_entry(hass)
        ent_reg = er.async_get(hass)
        assert (
            ent_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_bat1_voltage"
            )
            is None
        )

    async def test_battery_sensors_added_after_first_message(
        self, hass: HomeAssistant
    ) -> None:
        """Battery sensors must be dynamically created once the first payload arrives."""
        entry, coordinator = await _create_entry(hass)
        ent_reg = er.async_get(hass)

        coordinator._process_payload(_PAYLOAD)
        await hass.async_block_till_done()

        assert (
            ent_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_bat1_voltage"
            )
            is not None
        )

    async def test_cell_sensors_added_after_first_message(
        self, hass: HomeAssistant
    ) -> None:
        """Cell sensors must be dynamically created when cell data is in the payload."""
        entry, coordinator = await _create_entry(hass)
        ent_reg = er.async_get(hass)

        coordinator._process_payload(_PAYLOAD)
        await hass.async_block_till_done()

        assert (
            ent_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_bat1_cell0_voltage"
            )
            is not None
        )

    async def test_system_sensor_state_reflects_payload(
        self, hass: HomeAssistant
    ) -> None:
        """The system voltage sensor must report the value from the latest payload."""
        entry, coordinator = await _create_entry(hass)
        ent_reg = er.async_get(hass)

        coordinator._process_payload(_PAYLOAD)
        await hass.async_block_till_done()

        entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_voltage"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == pytest.approx(51.2)

    async def test_battery_sensor_state_reflects_payload(
        self, hass: HomeAssistant
    ) -> None:
        """Battery SOC sensor must report the value from the latest payload."""
        entry, coordinator = await _create_entry(hass)
        ent_reg = er.async_get(hass)

        coordinator._process_payload(_PAYLOAD)
        await hass.async_block_till_done()

        entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_bat1_soc"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == pytest.approx(80.0)

    async def test_second_payload_updates_sensor_state(
        self, hass: HomeAssistant
    ) -> None:
        """A second MQTT payload must update the sensor state to the new value."""
        entry, coordinator = await _create_entry(hass)
        ent_reg = er.async_get(hass)

        coordinator._process_payload(_PAYLOAD)
        await hass.async_block_till_done()

        updated_payload = {**_PAYLOAD, "voltage": 52.0}
        coordinator._process_payload(updated_payload)
        await hass.async_block_till_done()

        entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_voltage"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == pytest.approx(52.0)


class TestUnloadEntry:
    async def test_unload_removes_coordinator_from_hass_data(
        self, hass: HomeAssistant
    ) -> None:
        """After unloading, the coordinator must be gone from hass.data."""
        entry, _ = await _create_entry(hass)
        assert entry.entry_id in hass.data[DOMAIN]

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.entry_id not in hass.data.get(DOMAIN, {})

    async def test_unload_calls_coordinator_shutdown(self, hass: HomeAssistant) -> None:
        """Unloading must invoke coordinator.shutdown() to stop the MQTT thread."""
        from unittest.mock import patch as _patch

        entry, coordinator = await _create_entry(hass)

        with _patch.object(coordinator, "shutdown") as mock_shutdown:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()

        mock_shutdown.assert_called_once()

    async def test_reload_re_registers_sensors(self, hass: HomeAssistant) -> None:
        """After reload, the system voltage sensor must still be registered."""
        entry, coordinator = await _create_entry(hass)
        coordinator._process_payload(_PAYLOAD)
        await hass.async_block_till_done()

        with patch(_PATCH_SETUP):
            await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()

        reloaded_entries = hass.config_entries.async_entries(DOMAIN)
        assert reloaded_entries
        new_entry = reloaded_entries[0]
        ent_reg = er.async_get(hass)
        assert (
            ent_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{new_entry.entry_id}_voltage"
            )
            is not None
        )
