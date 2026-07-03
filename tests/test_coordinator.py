"""Tests for PylontechCoordinator business logic.

All tests use the ``hass`` fixture from pytest-homeassistant-custom-component
which provides a real, running HomeAssistant instance on the asyncio event loop.
The coordinator's MQTT client is never started (no ``setup()`` call), so these
tests exercise the pure-logic methods in isolation.
"""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.pylontech_mqtt.coordinator import PylontechCoordinator

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_BAT1: dict = {
    "sys_id": 1,
    "voltage": 51.2,
    "current": 10.0,
    "temperature": 25.0,
    "soc": 80,
    "status": "Charge",
    "power": 512.0,
    "raw": "",
    "cells": [],
}

_PAYLOAD: dict = {
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
async def coordinator(hass: HomeAssistant) -> PylontechCoordinator:
    """Coordinator wired to the test HA instance, MQTT client not started."""
    return PylontechCoordinator(
        hass=hass,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user="",
        mqtt_pass="",
        topic_prefix="pylontech/stack",
    )


# ---------------------------------------------------------------------------
# _deserialize
# ---------------------------------------------------------------------------


class TestDeserialize:
    async def test_returns_pylontech_system(
        self, coordinator: PylontechCoordinator
    ) -> None:
        assert isinstance(coordinator._deserialize(_PAYLOAD), dict)

    async def test_scalar_fields(self, coordinator: PylontechCoordinator) -> None:
        s = coordinator._deserialize(_PAYLOAD)
        assert s["voltage"] == 51.2
        assert s["current"] == 10.0
        assert s["soc"] == 80.0
        assert s["energy_in"] == 10.5
        assert s["energy_out"] == 5.2

    async def test_string_fields(self, coordinator: PylontechCoordinator) -> None:
        s = coordinator._deserialize(_PAYLOAD)
        assert s["spec"] == "48V/100AH"
        assert s["manufacturer"] == "Pylon"
        assert s["model"] == "US5KBPL"

    async def test_battery_count(self, coordinator: PylontechCoordinator) -> None:
        assert len(coordinator._deserialize(_PAYLOAD)["batteries"]) == 1

    async def test_battery_fields(self, coordinator: PylontechCoordinator) -> None:
        bat = coordinator._deserialize(_PAYLOAD)["batteries"][0]
        assert isinstance(bat, dict)
        assert bat["sys_id"] == 1
        assert bat["voltage"] == 51.2
        assert bat["soc"] == 80
        assert bat["status"] == "Charge"

    async def test_empty_payload_defaults(
        self, coordinator: PylontechCoordinator
    ) -> None:
        s = coordinator._deserialize({})
        assert s["voltage"] == 0
        assert s["soc"] == 0
        assert s["batteries"] == []
        assert s.get("spec") is None

    async def test_multiple_batteries(self, coordinator: PylontechCoordinator) -> None:
        payload = {**_PAYLOAD, "batteries": [_BAT1, {**_BAT1, "sys_id": 2, "soc": 60}]}
        s = coordinator._deserialize(payload)
        assert len(s["batteries"]) == 2
        assert s["batteries"][1]["sys_id"] == 2
        assert s["batteries"][1]["soc"] == 60

    async def test_energy_stored_initialised_zero(
        self, coordinator: PylontechCoordinator
    ) -> None:
        s = coordinator._deserialize(_PAYLOAD)
        assert s["energy_stored"] == 0.0
        assert s["batteries"][0]["energy_stored"] == 0.0

    async def test_cells_populated(self, coordinator: PylontechCoordinator) -> None:
        cell = {
            "cell_id": 0,
            "voltage": 3.4,
            "current": 0.5,
            "temperature": 25.0,
            "base_state": "Charge",
            "soc": 80,
        }
        payload = {**_PAYLOAD, "batteries": [{**_BAT1, "cells": [cell]}]}
        s = coordinator._deserialize(payload)
        cells = s["batteries"][0]["cells"]
        assert len(cells) == 1
        assert isinstance(cells[0], dict)
        assert cells[0]["voltage"] == 3.4

    async def test_optional_stat_fields_absent(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """Stat fields not in payload stay None rather than raising KeyError."""
        s = coordinator._deserialize(_PAYLOAD)
        assert s.get("cycles") is None
        assert s.get("soh") is None
        assert s.get("sc_times") is None


# ---------------------------------------------------------------------------
# _compute_energy_stored
# ---------------------------------------------------------------------------


class TestComputeEnergyStored:
    async def test_default_capacity(self, coordinator: PylontechCoordinator) -> None:
        """default_capacity=2.4 kWh, soc=80 % → 1.920 kWh."""
        s = coordinator._deserialize(_PAYLOAD)
        coordinator._compute_energy_stored(s)
        assert s["batteries"][0]["energy_stored"] == pytest.approx(1.920, rel=1e-3)

    async def test_system_total_equals_sum(
        self, coordinator: PylontechCoordinator
    ) -> None:
        payload = {**_PAYLOAD, "batteries": [_BAT1, {**_BAT1, "sys_id": 2, "soc": 60}]}
        s = coordinator._deserialize(payload)
        coordinator._compute_energy_stored(s)
        expected = (
            s["batteries"][0]["energy_stored"] + s["batteries"][1]["energy_stored"]
        )
        assert s["energy_stored"] == pytest.approx(expected, rel=1e-3)

    async def test_per_battery_capacity_override(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator.set_battery_capacity(1, 4.8)
        s = coordinator._deserialize(_PAYLOAD)
        coordinator._compute_energy_stored(s)
        assert s["batteries"][0]["energy_stored"] == pytest.approx(4.8 * 0.80, rel=1e-3)

    async def test_zero_soc(self, coordinator: PylontechCoordinator) -> None:
        s = coordinator._deserialize({**_PAYLOAD, "batteries": [{**_BAT1, "soc": 0}]})
        coordinator._compute_energy_stored(s)
        assert s["batteries"][0]["energy_stored"] == 0.0
        assert s["energy_stored"] == 0.0

    async def test_full_charge(self, coordinator: PylontechCoordinator) -> None:
        s = coordinator._deserialize({**_PAYLOAD, "batteries": [{**_BAT1, "soc": 100}]})
        coordinator._compute_energy_stored(s)
        assert s["batteries"][0]["energy_stored"] == pytest.approx(2.4, rel=1e-3)

    async def test_empty_batteries(self, coordinator: PylontechCoordinator) -> None:
        s = coordinator._deserialize({**_PAYLOAD, "batteries": []})
        coordinator._compute_energy_stored(s)
        assert s["energy_stored"] == 0.0

    async def test_second_battery_uses_its_own_capacity(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator.set_battery_capacity(1, 2.4)
        coordinator.set_battery_capacity(2, 4.8)
        payload = {**_PAYLOAD, "batteries": [_BAT1, {**_BAT1, "sys_id": 2, "soc": 50}]}
        s = coordinator._deserialize(payload)
        coordinator._compute_energy_stored(s)
        assert s["batteries"][0]["energy_stored"] == pytest.approx(2.4 * 0.80, rel=1e-3)
        assert s["batteries"][1]["energy_stored"] == pytest.approx(4.8 * 0.50, rel=1e-3)


# ---------------------------------------------------------------------------
# set_battery_capacity
# ---------------------------------------------------------------------------


class TestSetBatteryCapacity:
    async def test_configured_capacity_applied_in_computation(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """set_battery_capacity affects the energy_stored produced for that battery."""
        coordinator.set_battery_capacity(1, 4.8)
        coordinator._process_payload(_PAYLOAD)  # bat 1, soc=80
        assert coordinator.data["batteries"][0]["energy_stored"] == pytest.approx(
            4.8 * 0.80, rel=1e-3
        )

    async def test_later_value_overrides_earlier_for_same_battery(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator.set_battery_capacity(1, 2.4)
        coordinator.set_battery_capacity(1, 4.8)
        coordinator._process_payload(_PAYLOAD)  # bat 1, soc=80
        assert coordinator.data["batteries"][0]["energy_stored"] == pytest.approx(
            4.8 * 0.80, rel=1e-3
        )

    async def test_each_battery_uses_its_own_configured_capacity(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator.set_battery_capacity(1, 2.4)
        coordinator.set_battery_capacity(2, 4.8)
        payload = {
            **_PAYLOAD,
            "batteries": [_BAT1, {**_BAT1, "sys_id": 2, "soc": 50}],
        }
        coordinator._process_payload(payload)
        assert coordinator.data["batteries"][0]["energy_stored"] == pytest.approx(
            2.4 * 0.80, rel=1e-3
        )
        assert coordinator.data["batteries"][1]["energy_stored"] == pytest.approx(
            4.8 * 0.50, rel=1e-3
        )


# ---------------------------------------------------------------------------
# Auto-capacity detection via _process_payload
# ---------------------------------------------------------------------------


class TestAutoCapacity:
    async def test_initial_state(self, coordinator: PylontechCoordinator) -> None:
        assert coordinator.default_capacity == pytest.approx(2.4)

    async def test_us5000_spec(self, coordinator: PylontechCoordinator) -> None:
        coordinator._process_payload(_PAYLOAD)
        assert coordinator.default_capacity == pytest.approx(4.8)

    async def test_us2000_spec(self, coordinator: PylontechCoordinator) -> None:
        coordinator._process_payload({**_PAYLOAD, "spec": "48V/50AH"})
        assert coordinator.default_capacity == pytest.approx(2.4)

    async def test_us3000_spec(self, coordinator: PylontechCoordinator) -> None:
        coordinator._process_payload({**_PAYLOAD, "spec": "48V/74AH"})
        assert coordinator.default_capacity == pytest.approx(3.55, rel=1e-2)

    async def test_set_only_on_first_payload(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """A second payload with a different spec must not override the first derived value."""
        coordinator._process_payload(_PAYLOAD)  # 4.8 kWh
        coordinator._process_payload(
            {**_PAYLOAD, "spec": "48V/50AH"}
        )  # should not change
        assert coordinator.default_capacity == pytest.approx(4.8)

    async def test_absent_spec_leaves_default(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload({**_PAYLOAD, "spec": None})
        assert coordinator.default_capacity == pytest.approx(2.4)

    async def test_unparseable_spec_leaves_default(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload({**_PAYLOAD, "spec": "CUSTOM"})
        assert coordinator.default_capacity == pytest.approx(2.4)

    async def test_process_payload_updates_coordinator_data(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload(_PAYLOAD)
        assert coordinator.data is not None
        assert isinstance(coordinator.data, dict)
        assert coordinator.data["manufacturer"] == "Pylon"

    async def test_process_payload_computes_energy(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload(_PAYLOAD)
        assert coordinator.data["batteries"][0]["energy_stored"] > 0
        assert coordinator.data["energy_stored"] > 0
