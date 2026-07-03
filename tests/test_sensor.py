"""Unit tests for sensor native_value and entity device_info properties.

These tests instantiate entity objects directly (without going through HA's
config-entry loader) to give precise coverage of the value-reading paths that
are the primary output users see in the dashboard.
"""

import pytest
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

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

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
    "raw": "",
    "cells": [_CELL0],
}

_PAYLOAD = {
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def coord(hass: HomeAssistant) -> PylontechCoordinator:
    return PylontechCoordinator(
        hass=hass,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user="",
        mqtt_pass="",
        topic_prefix="test",
    )


@pytest.fixture
async def coord_with_data(coord: PylontechCoordinator) -> PylontechCoordinator:
    coord._process_payload(_PAYLOAD)
    return coord


# ---------------------------------------------------------------------------
# Entity factory helpers — create sensors without going through HA's loader.
# ---------------------------------------------------------------------------


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


# ===========================================================================
# PylontechSystemSensor.native_value
# ===========================================================================


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


# ===========================================================================
# PylontechBatterySensor.native_value
# ===========================================================================


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
        """energy_stored = spec-derived capacity(4.8 kWh for 48V/100AH) × soc(80%) = 3.84 kWh."""
        assert _bat(coord_with_data, "energy_stored").native_value == pytest.approx(
            3.84, rel=1e-3
        )

    async def test_missing_battery_id_returns_none(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert _bat(coord_with_data, "voltage", bat_id=99).native_value is None


# ===========================================================================
# PylontechCellSensor.native_value
# ===========================================================================


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


# ===========================================================================
# device_info (entity base classes)
# ===========================================================================


class TestDeviceInfo:
    # --- system-level device ---

    async def test_system_identifier(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert (DOMAIN, "system") in _sys(coord_with_data, "voltage").device_info.get(
            "identifiers", set()
        )

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

    # --- battery-module device ---

    async def test_battery_identifier(
        self, coord_with_data: PylontechCoordinator
    ) -> None:
        assert (DOMAIN, "battery_1") in _bat(
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
            "system",
        )

    # --- cell entity uses parent battery device ---

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
