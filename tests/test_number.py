"""Unit and integration tests for PylontechBatteryCapacityNumber.

Unit tests (TestNumberEntityAttributes, TestSetNativeValue) instantiate the
entity directly and mock out the HA write/refresh calls so they run without a
full integration load.

Integration tests (TestNumberEntityRegistration) go through the real config-
entry loader and verify the entity lifecycle matches the sensor platform
(absent before the first payload, present and correctly valued after it).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import create_config_entry, make_coordinator
from homeassistant.components.number import NumberDeviceClass, NumberMode
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.pylontech_mqtt.const import DOMAIN
from custom_components.pylontech_mqtt.coordinator import PylontechCoordinator
from custom_components.pylontech_mqtt.entity import stack_id_from_broker
from custom_components.pylontech_mqtt.number import PylontechBatteryCapacityNumber

# Shared test data

_BAT1: dict = {
    "sys_id": 1,
    "voltage": 51.2,
    "current": 10.0,
    "temperature": 25.0,
    "soc": 80,
    "status": "Charge",
    "power": 512.0,
    "cells": [],
}

_PAYLOAD: dict = {
    "schema_version": 1,
    "voltage": 51.2,
    "current": 10.0,
    "soc": 80.0,
    "power": 512.0,
    "energy_in": 10.5,
    "energy_out": 5.2,
    "spec": "48V/100AH",
    "manufacturer": "Pylon",
    "model": "US5KBPL",
    "batteries": [_BAT1],
}

_ENTRY_DATA: dict = {
    "mqtt_host": "localhost",
    "mqtt_port": 1883,
    "mqtt_user": "",
    "mqtt_pass": "",
    "mqtt_topic": "pylontech/stack",
}

# Entity identity is derived from host+port+topic (see
# entity.stack_id_from_broker).
_STACK_ID = stack_id_from_broker(
    _ENTRY_DATA["mqtt_host"], _ENTRY_DATA["mqtt_port"], _ENTRY_DATA["mqtt_topic"]
)

# Fixtures


@pytest.fixture
async def coordinator(hass: HomeAssistant) -> PylontechCoordinator:
    return make_coordinator(hass)


def _make_number(
    coord: PylontechCoordinator,
    bat_id: int = 1,
    entry_prefix: str = "entry_id",
) -> PylontechBatteryCapacityNumber:
    return PylontechBatteryCapacityNumber(coord, entry_prefix, bat_id)


# Entity attributes — guaranteed contract for the HA dashboard


class TestNumberEntityAttributes:
    def test_unit_is_kwh(self, coordinator: PylontechCoordinator) -> None:
        assert (
            _make_number(coordinator)._attr_native_unit_of_measurement
            == UnitOfEnergy.KILO_WATT_HOUR
        )

    def test_device_class_is_energy_storage(
        self, coordinator: PylontechCoordinator
    ) -> None:
        assert (
            _make_number(coordinator)._attr_device_class
            == NumberDeviceClass.ENERGY_STORAGE
        )

    def test_entity_category_is_config(self, coordinator: PylontechCoordinator) -> None:
        assert _make_number(coordinator)._attr_entity_category == EntityCategory.CONFIG

    def test_mode_is_box(self, coordinator: PylontechCoordinator) -> None:
        assert _make_number(coordinator)._attr_mode == NumberMode.BOX

    def test_min_value(self, coordinator: PylontechCoordinator) -> None:
        assert _make_number(coordinator)._attr_native_min_value == pytest.approx(0.5)

    def test_max_value(self, coordinator: PylontechCoordinator) -> None:
        assert _make_number(coordinator)._attr_native_max_value == pytest.approx(20.0)

    def test_step(self, coordinator: PylontechCoordinator) -> None:
        assert _make_number(coordinator)._attr_native_step == pytest.approx(0.1)

    def test_initial_value_matches_coordinator_default(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """Entity must initialise to the coordinator default so the UI is pre-filled."""
        assert _make_number(coordinator)._attr_native_value == pytest.approx(
            coordinator.default_capacity
        )

    def test_unique_id_encodes_entry_prefix_and_battery_id(
        self, coordinator: PylontechCoordinator
    ) -> None:
        n = PylontechBatteryCapacityNumber(coordinator, "pfx", 3)
        assert n._attr_unique_id == "pfx_bat3_capacity"

    def test_unavailable_when_battery_not_in_payload(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """The capacity number inherits PylontechBatteryEntity.available, so
        a module missing from the payload can't be configured as if it were
        still there."""
        coordinator.last_update_success = True
        coordinator._process_payload(_PAYLOAD)  # only bat 1
        assert _make_number(coordinator, bat_id=1).available is True
        assert _make_number(coordinator, bat_id=2).available is False


# async_set_native_value — coordinator side-effects


class TestSetNativeValue:
    async def test_updates_coordinator_battery_capacity(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """Setting a value must write the new capacity into battery_capacities."""
        n = _make_number(coordinator)
        n.async_write_ha_state = MagicMock()  # pyright: ignore[reportAttributeAccessIssue]
        coordinator.async_request_refresh = AsyncMock()

        await n.async_set_native_value(7.2)

        assert coordinator.battery_capacities[1] == pytest.approx(7.2)

    async def test_updates_native_value_attribute(
        self, coordinator: PylontechCoordinator
    ) -> None:
        n = _make_number(coordinator)
        n.async_write_ha_state = MagicMock()  # pyright: ignore[reportAttributeAccessIssue]
        coordinator.async_request_refresh = AsyncMock()

        await n.async_set_native_value(5.5)

        assert n._attr_native_value == pytest.approx(5.5)

    async def test_calls_async_write_ha_state(
        self, coordinator: PylontechCoordinator
    ) -> None:
        n = _make_number(coordinator)
        write = MagicMock()
        n.async_write_ha_state = write  # pyright: ignore[reportAttributeAccessIssue]
        coordinator.async_request_refresh = AsyncMock()

        await n.async_set_native_value(3.0)

        write.assert_called_once()

    async def test_triggers_coordinator_refresh(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """A refresh must be requested so energy_stored sensors update immediately."""
        n = _make_number(coordinator)
        n.async_write_ha_state = MagicMock()  # pyright: ignore[reportAttributeAccessIssue]
        refresh = AsyncMock()
        coordinator.async_request_refresh = refresh

        await n.async_set_native_value(4.0)

        refresh.assert_awaited_once()

    async def test_new_capacity_used_in_next_energy_computation(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """Capacity set via the number entity must be reflected in energy_stored."""
        n = _make_number(coordinator)
        n.async_write_ha_state = MagicMock()  # pyright: ignore[reportAttributeAccessIssue]
        coordinator.async_request_refresh = AsyncMock()

        await n.async_set_native_value(6.0)
        coordinator._process_payload(_PAYLOAD)  # bat1 soc=80

        assert coordinator.data["batteries"][0]["energy_stored"] == pytest.approx(
            6.0 * 0.80, rel=1e-3
        )


# Dynamic entity creation and HA state (integration-level)


async def _create_entry(hass: HomeAssistant):
    return await create_config_entry(hass, _ENTRY_DATA)


class TestNumberEntityRegistration:
    @pytest.fixture(autouse=True)
    def _enable(self, enable_custom_integrations) -> None:
        """Enable custom integration discovery for this class."""

    async def test_no_number_entity_before_first_payload(
        self, hass: HomeAssistant
    ) -> None:
        """Number entities must not exist before any MQTT payload arrives."""
        entry, _ = await _create_entry(hass)
        ent_reg = er.async_get(hass)
        assert (
            ent_reg.async_get_entity_id("number", DOMAIN, f"{_STACK_ID}_bat1_capacity")
            is None
        )

    async def test_number_entity_added_after_first_payload(
        self, hass: HomeAssistant
    ) -> None:
        """A capacity number entity must appear once battery data arrives."""
        entry, coordinator = await _create_entry(hass)
        ent_reg = er.async_get(hass)

        coordinator._process_payload(_PAYLOAD)
        await hass.async_block_till_done()

        assert (
            ent_reg.async_get_entity_id("number", DOMAIN, f"{_STACK_ID}_bat1_capacity")
            is not None
        )

    async def test_number_entity_initial_state_equals_derived_capacity(
        self, hass: HomeAssistant
    ) -> None:
        """Initial state must reflect the capacity auto-derived from the spec field."""
        entry, coordinator = await _create_entry(hass)
        ent_reg = er.async_get(hass)

        coordinator._process_payload(_PAYLOAD)  # spec="48V/100AH" → 4.8 kWh
        await hass.async_block_till_done()

        entity_id = ent_reg.async_get_entity_id(
            "number", DOMAIN, f"{_STACK_ID}_bat1_capacity"
        )
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == pytest.approx(coordinator.default_capacity)

    async def test_second_battery_gets_own_number_entity(
        self, hass: HomeAssistant
    ) -> None:
        """Each battery module must receive its own capacity number entity."""
        entry, coordinator = await _create_entry(hass)
        ent_reg = er.async_get(hass)

        coordinator._process_payload(
            {**_PAYLOAD, "batteries": [_BAT1, {**_BAT1, "sys_id": 2, "soc": 60}]}
        )
        await hass.async_block_till_done()

        assert (
            ent_reg.async_get_entity_id("number", DOMAIN, f"{_STACK_ID}_bat1_capacity")
            is not None
        )
        assert (
            ent_reg.async_get_entity_id("number", DOMAIN, f"{_STACK_ID}_bat2_capacity")
            is not None
        )

    async def test_repeated_payload_does_not_duplicate_number_entity(
        self, hass: HomeAssistant
    ) -> None:
        """A second payload for the same battery must not register a duplicate."""
        entry, coordinator = await _create_entry(hass)
        ent_reg = er.async_get(hass)

        coordinator._process_payload(_PAYLOAD)
        await hass.async_block_till_done()
        coordinator._process_payload(_PAYLOAD)
        await hass.async_block_till_done()

        entities = [
            e
            for e in ent_reg.entities.values()
            if e.platform == DOMAIN and e.domain == "number"
        ]
        bat1_entities = [e for e in entities if "bat1_capacity" in e.unique_id]
        assert len(bat1_entities) == 1
