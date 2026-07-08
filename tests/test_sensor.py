"""Unit tests for sensor native_value and entity device_info properties.

These tests instantiate entity objects directly (without going through HA's
config-entry loader) to give precise coverage of the value-reading paths that
are the primary output users see in the dashboard.
"""

import pytest
from conftest import make_coordinator
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant

from custom_components.pylontech_mqtt.const import DOMAIN
from custom_components.pylontech_mqtt.coordinator import PylontechCoordinator
from custom_components.pylontech_mqtt.sensor import (
    BATTERY_SENSORS,
    CELL_SENSORS,
    SYSTEM_SENSORS,
    PylontechBatterySensor,
    PylontechCellSensor,
    PylontechSystemSensor,
)

# Shared test data

_CELL0 = {
    "cell_id": 0,
    "voltage": 3.4,
    "current": 0.5,
    "temperature": 25.0,
    "base_state": "Charge",
    "soc": 80,
}

_BAT1 = {
    "sys_id": 1,
    "voltage": 51.2,
    "current": 10.0,
    "temperature": 25.0,
    "soc": 80,
    "status": "Charge",
    "power": 512.0,
    "cells": [_CELL0],
}

_PAYLOAD = {
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

# Fixtures


@pytest.fixture
async def coord(hass: HomeAssistant) -> PylontechCoordinator:
    return make_coordinator(hass)


@pytest.fixture
async def coord_with_data(coord: PylontechCoordinator) -> PylontechCoordinator:
    coord._process_payload(_PAYLOAD)
    return coord


# Entity factory helpers — create sensors without going through HA's loader.


def _sys(coord: PylontechCoordinator, key: str) -> PylontechSystemSensor:
    desc = next(d for d in SYSTEM_SENSORS if d.key == key)
    return PylontechSystemSensor(coord, "entry_id", desc)


def _bat(
    coord: PylontechCoordinator, key: str, bat_id: int = 1
) -> PylontechBatterySensor:
    desc = next(d for d in BATTERY_SENSORS if d.key == key)
    return PylontechBatterySensor(coord, "entry_id", bat_id, desc)


def _cell(
    coord: PylontechCoordinator,
    key: str,
    bat_id: int = 1,
    cell_id: int = 0,
) -> PylontechCellSensor:
    desc = next(d for d in CELL_SENSORS if d.key == key)
    return PylontechCellSensor(coord, "entry_id", bat_id, cell_id, desc)


# PylontechSystemSensor.native_value


class TestSystemSensorNativeValue:
    async def test_none_before_first_message(self, coord: PylontechCoordinator) -> None:
        assert _sys(coord, "voltage").native_value is None

    async def test_voltage(self, coord_with_data: PylontechCoordinator) -> None:
        assert _sys(coord_with_data, "voltage").native_value == 51.2

    async def test_current(self, coord_with_data: PylontechCoordinator) -> None:
        assert _sys(coord_with_data, "current").native_value == 10.0

    async def test_soc(self, coord_with_data: PylontechCoordinator) -> None:
        assert _sys(coord_with_data, "soc").native_value == 80.0

    async def test_optional_string_field(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert _sys(coord_with_data, "spec").native_value == "48V/100AH"

    async def test_absent_stat_field_is_none(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        """cycles is not in _PAYLOAD → stays None."""
        assert _sys(coord_with_data, "cycles").native_value is None


# PylontechBatterySensor.native_value


class TestBatterySensorNativeValue:
    async def test_none_before_first_message(self, coord: PylontechCoordinator) -> None:
        assert _bat(coord, "voltage").native_value is None

    async def test_voltage(self, coord_with_data: PylontechCoordinator) -> None:
        assert _bat(coord_with_data, "voltage").native_value == 51.2

    async def test_soc(self, coord_with_data: PylontechCoordinator) -> None:
        assert _bat(coord_with_data, "soc").native_value == 80

    async def test_status_string(self, coord_with_data: PylontechCoordinator) -> None:
        assert _bat(coord_with_data, "status").native_value == "Charge"

    async def test_energy_stored_computed(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        """energy_stored = spec capacity(4.8 kWh, 48V/100AH) × soc(80%) = 3.84 kWh."""
        assert _bat(coord_with_data, "energy_stored").native_value == pytest.approx(
            3.84, rel=1e-3
        )

    async def test_missing_battery_id_returns_none(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert _bat(coord_with_data, "voltage", bat_id=99).native_value is None


# PylontechCellSensor.native_value


class TestCellSensorNativeValue:
    async def test_none_before_first_message(self, coord: PylontechCoordinator) -> None:
        assert _cell(coord, "voltage").native_value is None

    async def test_cell_voltage(self, coord_with_data: PylontechCoordinator) -> None:
        assert _cell(coord_with_data, "voltage").native_value == 3.4

    async def test_cell_soc(self, coord_with_data: PylontechCoordinator) -> None:
        assert _cell(coord_with_data, "soc").native_value == 80

    async def test_cell_base_state(self, coord_with_data: PylontechCoordinator) -> None:
        assert _cell(coord_with_data, "base_state").native_value == "Charge"

    async def test_missing_cell_id_returns_none(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert _cell(coord_with_data, "voltage", cell_id=99).native_value is None

    async def test_missing_battery_id_returns_none(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert _cell(coord_with_data, "voltage", bat_id=99).native_value is None


# device_info (entity base classes)


class TestDeviceInfo:
    # system-level device

    async def test_system_identifier(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert (
            DOMAIN,
            "entry_id_system",
        ) in _sys(coord_with_data, "voltage").device_info.get("identifiers", set())

    async def test_system_name(self, coord_with_data: PylontechCoordinator) -> None:
        assert (
            _sys(coord_with_data, "voltage").device_info.get("name")
            == "Pylontech Stack"
        )

    async def test_system_manufacturer_from_data(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert (
            _sys(coord_with_data, "voltage").device_info.get("manufacturer") == "Pylon"
        )

    async def test_system_manufacturer_fallback_when_no_data(
        self, coord: PylontechCoordinator
    ) -> None:
        assert _sys(coord, "voltage").device_info.get("manufacturer") == "Pylontech"

    async def test_system_no_via_device(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        """System is the root device — no via_device link."""
        assert "via_device" not in _sys(coord_with_data, "voltage").device_info

    # battery-module device

    async def test_battery_identifier(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert (DOMAIN, "entry_id_battery_1") in _bat(
            coord_with_data, "voltage"
        ).device_info.get("identifiers", set())

    async def test_battery_name_includes_id(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert (
            _bat(coord_with_data, "voltage", bat_id=3).device_info.get("name")
            == "Pylontech Module 3"
        )

    async def test_battery_via_device_links_to_system(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert _bat(coord_with_data, "voltage").device_info.get("via_device") == (
            DOMAIN,
            "entry_id_system",
        )

    async def test_different_entries_use_different_devices(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        desc = next(d for d in SYSTEM_SENSORS if d.key == "voltage")
        first = PylontechSystemSensor(coord_with_data, "entry_one", desc)
        second = PylontechSystemSensor(coord_with_data, "entry_two", desc)

        assert first.device_info.get("identifiers") != second.device_info.get(
            "identifiers"
        )

    # cell entity uses parent battery device

    async def test_cell_uses_battery_identifiers(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        """Cells attach to their parent battery device, not a separate device."""
        assert _cell(coord_with_data, "voltage").device_info.get("identifiers") == _bat(
            coord_with_data, "voltage"
        ).device_info.get("identifiers")

    async def test_cell_entity_name_includes_cell_id(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert _cell(coord_with_data, "voltage").name == "Cell 0 Voltage"


# Availability — a missing module/cell must report unavailable, not just
# freeze on stale values (PylontechBatteryEntity/PylontechCellEntity.available)


class TestAvailability:
    async def test_battery_available_when_present(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        coord_with_data.last_update_success = True
        assert _bat(coord_with_data, "voltage", bat_id=1).available is True

    async def test_battery_unavailable_when_dropped_from_payload(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        """Module 2 was never in _PAYLOAD (only module 1 is) — this is the
        same situation as a module dropping out between polls."""
        coord_with_data.last_update_success = True
        assert _bat(coord_with_data, "voltage", bat_id=2).available is False

    async def test_battery_unavailable_when_coordinator_unavailable(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        """Coordinator-level unavailability must still take priority even
        for a module that IS present in the last-known payload."""
        coord_with_data.last_update_success = False
        assert _bat(coord_with_data, "voltage", bat_id=1).available is False

    async def test_battery_unavailable_before_first_payload(
        self, coord: PylontechCoordinator
    ) -> None:
        coord.last_update_success = True
        assert _bat(coord, "voltage", bat_id=1).available is False

    async def test_cell_available_when_present(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        coord_with_data.last_update_success = True
        assert _cell(coord_with_data, "voltage", bat_id=1, cell_id=0).available is True

    async def test_cell_unavailable_when_dropped_from_payload(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        coord_with_data.last_update_success = True
        assert _cell(coord_with_data, "voltage", bat_id=1, cell_id=9).available is False

    async def test_cell_unavailable_when_parent_battery_absent(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        """A cell can't be available if its own module isn't reporting at all."""
        coord_with_data.last_update_success = True
        assert _cell(coord_with_data, "voltage", bat_id=2, cell_id=0).available is False

    async def test_system_availability_unaffected_by_battery_presence(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        """System-level entities track only coordinator availability — there
        is no per-module presence concept at the stack level."""
        coord_with_data.last_update_success = True
        assert _sys(coord_with_data, "voltage").available is True


# Sensor metadata — unit_of_measurement, device_class, state_class
# Protects the HA energy dashboard and device history from regressions.


class TestSensorMetadata:
    # System sensors

    def test_system_voltage_unit(self, coord: PylontechCoordinator) -> None:
        assert (
            _sys(coord, "voltage").native_unit_of_measurement
            == UnitOfElectricPotential.VOLT
        )

    def test_system_voltage_device_class(self, coord: PylontechCoordinator) -> None:
        assert _sys(coord, "voltage").device_class == SensorDeviceClass.VOLTAGE

    def test_system_voltage_state_class(self, coord: PylontechCoordinator) -> None:
        assert _sys(coord, "voltage").state_class == SensorStateClass.MEASUREMENT

    def test_system_current_unit(self, coord: PylontechCoordinator) -> None:
        assert (
            _sys(coord, "current").native_unit_of_measurement
            == UnitOfElectricCurrent.AMPERE
        )

    def test_system_soc_unit(self, coord: PylontechCoordinator) -> None:
        assert _sys(coord, "soc").native_unit_of_measurement == PERCENTAGE

    def test_system_soc_device_class(self, coord: PylontechCoordinator) -> None:
        assert _sys(coord, "soc").device_class == SensorDeviceClass.BATTERY

    def test_system_power_unit(self, coord: PylontechCoordinator) -> None:
        assert _sys(coord, "power").native_unit_of_measurement == UnitOfPower.WATT

    def test_system_power_device_class(self, coord: PylontechCoordinator) -> None:
        assert _sys(coord, "power").device_class == SensorDeviceClass.POWER

    def test_system_energy_in_unit(self, coord: PylontechCoordinator) -> None:
        assert (
            _sys(coord, "energy_in").native_unit_of_measurement
            == UnitOfEnergy.KILO_WATT_HOUR
        )

    def test_system_energy_in_device_class(self, coord: PylontechCoordinator) -> None:
        assert _sys(coord, "energy_in").device_class == SensorDeviceClass.ENERGY

    def test_system_energy_in_state_class_is_total_increasing(
        self, coord: PylontechCoordinator
    ) -> None:
        """energy_in must use TOTAL_INCREASING so a sidecar-restart reset is
        treated as a new meter cycle rather than corrupting long-term stats."""
        assert _sys(coord, "energy_in").state_class == SensorStateClass.TOTAL_INCREASING

    def test_system_energy_out_state_class_is_total_increasing(
        self, coord: PylontechCoordinator
    ) -> None:
        assert (
            _sys(coord, "energy_out").state_class == SensorStateClass.TOTAL_INCREASING
        )

    def test_system_energy_stored_unit(self, coord: PylontechCoordinator) -> None:
        assert (
            _sys(coord, "energy_stored").native_unit_of_measurement
            == UnitOfEnergy.KILO_WATT_HOUR
        )

    def test_system_energy_stored_device_class(
        self, coord: PylontechCoordinator
    ) -> None:
        assert (
            _sys(coord, "energy_stored").device_class
            == SensorDeviceClass.ENERGY_STORAGE
        )

    def test_system_energy_stored_state_class(
        self, coord: PylontechCoordinator
    ) -> None:
        assert _sys(coord, "energy_stored").state_class == SensorStateClass.MEASUREMENT

    # Battery sensors

    def test_battery_voltage_unit(self, coord: PylontechCoordinator) -> None:
        assert (
            _bat(coord, "voltage").native_unit_of_measurement
            == UnitOfElectricPotential.VOLT
        )

    def test_battery_voltage_device_class(self, coord: PylontechCoordinator) -> None:
        assert _bat(coord, "voltage").device_class == SensorDeviceClass.VOLTAGE

    def test_battery_current_unit(self, coord: PylontechCoordinator) -> None:
        assert (
            _bat(coord, "current").native_unit_of_measurement
            == UnitOfElectricCurrent.AMPERE
        )

    def test_battery_temperature_unit(self, coord: PylontechCoordinator) -> None:
        assert (
            _bat(coord, "temperature").native_unit_of_measurement
            == UnitOfTemperature.CELSIUS
        )

    def test_battery_temperature_device_class(
        self, coord: PylontechCoordinator
    ) -> None:
        assert _bat(coord, "temperature").device_class == SensorDeviceClass.TEMPERATURE

    def test_battery_soc_unit(self, coord: PylontechCoordinator) -> None:
        assert _bat(coord, "soc").native_unit_of_measurement == PERCENTAGE

    def test_battery_soc_device_class(self, coord: PylontechCoordinator) -> None:
        assert _bat(coord, "soc").device_class == SensorDeviceClass.BATTERY

    def test_battery_power_unit(self, coord: PylontechCoordinator) -> None:
        assert _bat(coord, "power").native_unit_of_measurement == UnitOfPower.WATT

    def test_battery_power_state_class(self, coord: PylontechCoordinator) -> None:
        assert _bat(coord, "power").state_class == SensorStateClass.MEASUREMENT

    # Cell sensors

    def test_cell_voltage_unit(self, coord: PylontechCoordinator) -> None:
        assert (
            _cell(coord, "voltage").native_unit_of_measurement
            == UnitOfElectricPotential.VOLT
        )

    def test_cell_voltage_device_class(self, coord: PylontechCoordinator) -> None:
        assert _cell(coord, "voltage").device_class == SensorDeviceClass.VOLTAGE

    def test_cell_voltage_state_class(self, coord: PylontechCoordinator) -> None:
        assert _cell(coord, "voltage").state_class == SensorStateClass.MEASUREMENT

    def test_cell_temperature_unit(self, coord: PylontechCoordinator) -> None:
        assert (
            _cell(coord, "temperature").native_unit_of_measurement
            == UnitOfTemperature.CELSIUS
        )

    def test_cell_soc_unit(self, coord: PylontechCoordinator) -> None:
        assert _cell(coord, "soc").native_unit_of_measurement == PERCENTAGE

    def test_cell_soc_device_class(self, coord: PylontechCoordinator) -> None:
        assert _cell(coord, "soc").device_class == SensorDeviceClass.BATTERY
