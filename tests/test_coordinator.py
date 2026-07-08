"""Tests for PylontechCoordinator business logic.

All tests use the ``hass`` fixture from pytest-homeassistant-custom-component
which provides a real, running HomeAssistant instance on the asyncio event loop.
The coordinator's MQTT client is never started (no ``setup()`` call), so these
tests exercise the pure-logic methods in isolation.
"""

import time
from datetime import datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import paho.mqtt.client as mqtt
import pytest
from conftest import make_coordinator
from homeassistant.core import HomeAssistant
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.pylontech_mqtt.coordinator import PylontechCoordinator

# _on_message/_on_disconnect/_check_staleness only touch a couple of fields
# (or none at all, for _check_staleness's `now`) off these paho-mqtt/datetime
# parameters, so tests use lightweight stand-ins cast to the declared type
# rather than constructing real paho objects.
_FAKE_CLIENT = cast(mqtt.Client, None)
_FAKE_NOW = cast(datetime, None)

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


# Fixtures


@pytest.fixture
async def coordinator(hass: HomeAssistant) -> PylontechCoordinator:
    """Coordinator wired to the test HA instance, MQTT client not started."""
    return make_coordinator(hass)


# _deserialize


class TestTlsSetup:
    """Whether setup() enables TLS on the paho client (see coordinator.setup)."""

    async def test_tls_enabled_calls_tls_set(self, hass: HomeAssistant) -> None:
        coord = make_coordinator(hass, mqtt_tls=True)
        with patch(
            "custom_components.pylontech_mqtt.coordinator.mqtt.Client"
        ) as mock_client_cls:
            coord.setup()
            mock_client_cls.return_value.tls_set.assert_called_once()
            coord.shutdown()
            await hass.async_block_till_done()

    async def test_tls_disabled_does_not_call_tls_set(
        self, hass: HomeAssistant
    ) -> None:
        coord = make_coordinator(hass, mqtt_tls=False)
        with patch(
            "custom_components.pylontech_mqtt.coordinator.mqtt.Client"
        ) as mock_client_cls:
            coord.setup()
            mock_client_cls.return_value.tls_set.assert_not_called()
            coord.shutdown()
            await hass.async_block_till_done()


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


# _compute_energy_stored


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

    async def test_battery_missing_sys_id_is_skipped(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """A battery dict without sys_id must be skipped, not raise KeyError."""
        # One valid battery (sys_id=1, soc=80) and one with no sys_id.
        no_id_bat = {k: v for k, v in _BAT1.items() if k != "sys_id"}
        s = coordinator._deserialize({**_PAYLOAD, "batteries": [_BAT1, no_id_bat]})
        # Must not raise; only the valid battery contributes to the total.
        coordinator._compute_energy_stored(s)
        assert s["energy_stored"] == pytest.approx(2.4 * 0.80, rel=1e-3)


# set_battery_capacity


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


# is_battery_present / is_cell_present


class TestBatteryAndCellPresence:
    async def test_absent_before_any_payload(
        self, coordinator: PylontechCoordinator
    ) -> None:
        assert coordinator.is_battery_present(1) is False
        assert coordinator.is_cell_present(1, 0) is False

    async def test_present_battery_after_payload(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload(_PAYLOAD)  # only bat 1
        assert coordinator.is_battery_present(1) is True

    async def test_unknown_battery_id_is_absent(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload(_PAYLOAD)  # only bat 1
        assert coordinator.is_battery_present(99) is False

    async def test_battery_dropped_between_payloads_becomes_absent(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """A module present in one payload but missing from the next
        (the sidecar drops "Absent" rows entirely) must read as absent —
        this is what lets entity availability track real module presence."""
        two_batteries = {
            **_PAYLOAD,
            "batteries": [_BAT1, {**_BAT1, "sys_id": 2}],
        }
        coordinator._process_payload(two_batteries)
        assert coordinator.is_battery_present(2) is True

        coordinator._process_payload(_PAYLOAD)  # bat 2 no longer reported
        assert coordinator.is_battery_present(2) is False
        assert coordinator.is_battery_present(1) is True

    async def test_present_cell(self, coordinator: PylontechCoordinator) -> None:
        cell = {"cell_id": 0, "voltage": 3.4, "soc": 80, "base_state": "Charge"}
        payload = {**_PAYLOAD, "batteries": [{**_BAT1, "cells": [cell]}]}
        coordinator._process_payload(payload)
        assert coordinator.is_cell_present(1, 0) is True

    async def test_cell_absent_when_its_battery_is_absent(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload(_PAYLOAD)  # bat 1 exists, no bat 2 at all
        assert coordinator.is_cell_present(2, 0) is False

    async def test_cell_dropped_between_payloads_becomes_absent(
        self, coordinator: PylontechCoordinator
    ) -> None:
        cell0 = {"cell_id": 0, "voltage": 3.4, "soc": 80, "base_state": "Charge"}
        cell1 = {"cell_id": 1, "voltage": 3.4, "soc": 80, "base_state": "Charge"}
        coordinator._process_payload(
            {**_PAYLOAD, "batteries": [{**_BAT1, "cells": [cell0, cell1]}]}
        )
        assert coordinator.is_cell_present(1, 1) is True

        coordinator._process_payload(
            {**_PAYLOAD, "batteries": [{**_BAT1, "cells": [cell0]}]}
        )
        assert coordinator.is_cell_present(1, 1) is False
        assert coordinator.is_cell_present(1, 0) is True


# Auto-capacity detection via _process_payload


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
        """A second payload with a different spec must not override the first value."""
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


# Availability lifecycle (_mark_available / _mark_unavailable / _on_message)


def _msg(topic: str, payload: str | bytes) -> mqtt.MQTTMessage:
    """Build a minimal paho-style message object for _on_message tests."""
    return cast(
        mqtt.MQTTMessage,
        SimpleNamespace(
            topic=topic,
            payload=payload if isinstance(payload, bytes) else payload.encode(),
        ),
    )


class TestAvailability:
    async def test_mark_unavailable_sets_flag(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator.last_update_success = True
        coordinator._mark_unavailable()
        assert coordinator.last_update_success is False

    async def test_mark_available_sets_flag(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator.last_update_success = False
        coordinator._mark_available()
        assert coordinator.last_update_success is True

    async def test_offline_message_marks_unavailable(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """Receiving 'offline' on the avail topic must mark coordinator unavailable."""
        coordinator.last_update_success = True
        coordinator._on_message(
            _FAKE_CLIENT, None, _msg("pylontech/stack/availability", "offline")
        )
        await hass.async_block_till_done()
        assert coordinator.last_update_success is False


# Staleness watchdog (_check_staleness) — catches a sidecar poll loop that
# hangs while its MQTT connection stays up, so no LWT/disconnect ever fires.


class TestStalenessWatchdog:
    async def test_availability_message_does_not_update_last_message_time(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """The sidecar republishes 'online' every poll cycle regardless of
        whether that cycle's state payload was valid, so it must not feed the
        staleness clock — otherwise a chronically incompatible/malformed
        publisher would dodge the watchdog forever while never delivering
        usable data (see coordinator._on_message)."""
        assert coordinator._last_message_monotonic is None
        coordinator._on_message(
            _FAKE_CLIENT, None, _msg("pylontech/stack/availability", "online")
        )
        assert coordinator._last_message_monotonic is None

    async def test_valid_state_message_updates_last_message_time(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        assert coordinator._last_message_monotonic is None
        coordinator._process_payload(_PAYLOAD)
        assert coordinator._last_message_monotonic is not None

    async def test_invalid_state_message_does_not_update_last_message_time(
        self, coordinator: PylontechCoordinator
    ) -> None:
        assert coordinator._last_message_monotonic is None
        coordinator._process_payload({})  # missing required fields
        assert coordinator._last_message_monotonic is None

    async def test_stale_last_message_marks_unavailable(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator.last_update_success = True
        coordinator._last_message_monotonic = (
            time.monotonic() - coordinator._STALE_TIMEOUT_SECONDS - 1
        )
        coordinator._check_staleness(_FAKE_NOW)
        assert coordinator.last_update_success is False

    async def test_recent_last_message_stays_available(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator.last_update_success = True
        coordinator._last_message_monotonic = time.monotonic() - 5
        coordinator._check_staleness(_FAKE_NOW)
        assert coordinator.last_update_success is True

    async def test_no_message_ever_received_does_not_crash(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """Before the first message, _last_message_monotonic is None — the
        watchdog must be a no-op, not raise on the None subtraction."""
        assert coordinator._last_message_monotonic is None
        coordinator._check_staleness(_FAKE_NOW)
        assert coordinator.last_update_success is False

    async def test_already_unavailable_is_left_alone(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """No point re-marking or re-warning about an already-unavailable
        coordinator — the guard should skip work entirely."""
        coordinator.last_update_success = False
        coordinator._last_message_monotonic = (
            time.monotonic() - coordinator._STALE_TIMEOUT_SECONDS - 1
        )
        coordinator._check_staleness(_FAKE_NOW)
        assert coordinator.last_update_success is False

    async def test_setup_registers_watchdog_and_shutdown_cancels_it(
        self, hass: HomeAssistant
    ) -> None:
        coord = make_coordinator(hass)
        with (
            patch("custom_components.pylontech_mqtt.coordinator.mqtt.Client"),
            patch(
                "custom_components.pylontech_mqtt.coordinator.async_track_time_interval"
            ) as mock_track,
        ):
            mock_unsub = mock_track.return_value
            coord.setup()
            mock_track.assert_called_once()
            assert mock_track.call_args.args[1] == coord._check_staleness

            coord.shutdown()
            await hass.async_block_till_done()
            mock_unsub.assert_called_once()

    async def test_online_after_offline_restores_available(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """'online' after 'offline' must restore availability if data exists."""
        coordinator._process_payload(_PAYLOAD)  # populate coordinator.data
        coordinator._mark_unavailable()  # simulate sidecar going offline
        assert coordinator.last_update_success is False

        coordinator._on_message(
            _FAKE_CLIENT, None, _msg("pylontech/stack/availability", "online")
        )
        await hass.async_block_till_done()
        assert coordinator.last_update_success is True

    async def test_online_without_data_marks_available(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """'online' before any state message must immediately mark the device available.

        The availability and state topics are independent; gating the 'online'
        signal on data being present creates a race where the device appears
        offline even though the sidecar already published 'online'.
        """
        assert coordinator.data is None
        coordinator._on_message(
            _FAKE_CLIENT, None, _msg("pylontech/stack/availability", "online")
        )
        await hass.async_block_till_done()
        # Device should be marked available regardless of whether data has arrived.
        assert coordinator.last_update_success is True

    async def test_online_does_not_restore_availability_while_data_is_stale(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """Once the watchdog has marked the device unavailable for staleness,
        the sidecar's next periodic 'online' republish (sent every poll cycle
        regardless of that cycle's own payload validity) must NOT flip
        availability back on by itself — only a fresh, successfully
        validated state payload may do that. Otherwise stale/rejected data
        could repeatedly look "available" forever."""
        coordinator._process_payload(_PAYLOAD)  # populate data, set the clock
        coordinator._last_message_monotonic = (
            time.monotonic() - coordinator._STALE_TIMEOUT_SECONDS - 1
        )
        coordinator._check_staleness(_FAKE_NOW)  # watchdog fires
        assert coordinator.last_update_success is False

        coordinator._on_message(
            _FAKE_CLIENT, None, _msg("pylontech/stack/availability", "online")
        )
        await hass.async_block_till_done()
        assert coordinator.last_update_success is False

    async def test_online_restores_availability_once_fresh_data_arrives(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """After a staleness-driven unavailable, a genuinely fresh valid state
        payload (not just an 'online' republish) must restore availability."""
        coordinator._process_payload(_PAYLOAD)
        coordinator._last_message_monotonic = (
            time.monotonic() - coordinator._STALE_TIMEOUT_SECONDS - 1
        )
        coordinator._check_staleness(_FAKE_NOW)
        assert coordinator.last_update_success is False

        coordinator._process_payload(_PAYLOAD)
        assert coordinator.last_update_success is True

    async def test_unrecognised_avail_payload_marks_unavailable(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """Any payload other than 'online' must be treated as unavailable."""
        coordinator.last_update_success = True
        coordinator._on_message(
            _FAKE_CLIENT, None, _msg("pylontech/stack/availability", "unknown")
        )
        await hass.async_block_till_done()
        assert coordinator.last_update_success is False

    async def test_disconnect_marks_unavailable(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """An MQTT disconnect must immediately mark the coordinator unavailable.

        paho-mqtt reconnects automatically; on reconnect the broker re-delivers
        the retained availability payload to restore the correct state.  During
        the reconnect window HA must reflect the loss of comms.
        """
        coordinator.last_update_success = True
        from types import SimpleNamespace

        coordinator._on_disconnect(
            _FAKE_CLIENT,
            None,
            cast(mqtt.DisconnectFlags, SimpleNamespace()),
            cast(ReasonCode, 0),
            None,
        )
        await hass.async_block_till_done()
        assert coordinator.last_update_success is False

    async def test_state_message_not_dispatched_to_availability_handler(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """A valid state JSON on the state topic must populate data, not avail flag."""
        import json

        coordinator.last_update_success = False
        coordinator._on_message(
            _FAKE_CLIENT, None, _msg("pylontech/stack/state", json.dumps(_PAYLOAD))
        )
        await hass.async_block_till_done()
        assert coordinator.data is not None
        assert coordinator.last_update_success is True


# _on_message error handling — malformed payloads


class TestOnMessageErrors:
    async def test_non_utf8_avail_payload_treated_as_offline(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """A non-UTF-8 payload on the availability topic must be treated as offline."""
        coordinator.last_update_success = True
        coordinator._on_message(
            _FAKE_CLIENT, None, _msg("pylontech/stack/availability", b"\xff\xfe")
        )
        await hass.async_block_till_done()
        assert coordinator.last_update_success is False

    async def test_invalid_json_state_payload_does_not_crash(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """An invalid JSON payload on the state topic must not raise or update state."""
        coordinator._on_message(
            _FAKE_CLIENT, None, _msg("pylontech/stack/state", b"not-json{}")
        )
        await hass.async_block_till_done()
        assert coordinator.data is None

    async def test_non_utf8_state_payload_does_not_crash(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """A non-UTF-8 payload on the state topic must not raise."""
        coordinator._on_message(
            _FAKE_CLIENT, None, _msg("pylontech/stack/state", b"\xff\xfe")
        )
        await hass.async_block_till_done()
        assert coordinator.data is None

    async def test_non_dict_json_payload_is_rejected(
        self, hass: HomeAssistant, coordinator: PylontechCoordinator
    ) -> None:
        """A valid JSON payload that is not a dict (e.g. null, list, int) must
        be logged and dropped without updating coordinator.data."""

        for bad_value in ("null", "[]", "123"):
            coordinator._on_message(
                _FAKE_CLIENT, None, _msg("pylontech/stack/state", bad_value)
            )
        await hass.async_block_till_done()
        assert coordinator.data is None


# _process_payload schema validation — a partial/incompatible publisher must
# never be allowed to overwrite good live data with zero-filled readings.


class TestPayloadSchemaValidation:
    async def test_empty_payload_is_rejected_not_zero_filled(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """An empty {} must be dropped outright, not accepted as all-zero
        voltage/soc/power/energy — that would silently corrupt live data."""
        coordinator._process_payload({})
        assert coordinator.data is None

    async def test_empty_payload_does_not_clobber_existing_good_data(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload(_PAYLOAD)
        assert coordinator.data["voltage"] == 51.2

        coordinator._process_payload({})  # malformed follow-up message

        assert coordinator.data["voltage"] == 51.2

    async def test_missing_schema_version_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """A sidecar predating schema_version (or one that omits it) must be
        treated as incompatible, not silently accepted."""
        payload = {k: v for k, v in _PAYLOAD.items() if k != "schema_version"}
        coordinator._process_payload(payload)
        assert coordinator.data is None

    async def test_mismatched_schema_version_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload({**_PAYLOAD, "schema_version": 999})
        assert coordinator.data is None

    @pytest.mark.parametrize(
        "missing_field",
        [
            "voltage",
            "current",
            "soc",
            "power",
            "energy_in",
            "energy_out",
            "batteries",
        ],
    )
    async def test_missing_top_level_field_is_rejected(
        self, coordinator: PylontechCoordinator, missing_field: str
    ) -> None:
        payload = {k: v for k, v in _PAYLOAD.items() if k != missing_field}
        coordinator._process_payload(payload)
        assert coordinator.data is None

    @pytest.mark.parametrize(
        "field,bad_value",
        [
            ("voltage", "51.2"),  # string, not numeric
            ("voltage", float("nan")),
            ("voltage", float("inf")),
            ("voltage", -1),  # out of range
            ("soc", 150),  # out of range
            ("soc", -5),
            ("power", True),  # bool must not pass as numeric
        ],
    )
    async def test_invalid_top_level_value_is_rejected(
        self, coordinator: PylontechCoordinator, field: str, bad_value
    ) -> None:
        coordinator._process_payload({**_PAYLOAD, field: bad_value})
        assert coordinator.data is None

    async def test_batteries_not_a_list_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload({**_PAYLOAD, "batteries": {"sys_id": 1}})
        assert coordinator.data is None

    async def test_battery_missing_required_field_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        bad_bat = {k: v for k, v in _BAT1.items() if k != "voltage"}
        coordinator._process_payload({**_PAYLOAD, "batteries": [bad_bat]})
        assert coordinator.data is None

    async def test_battery_non_int_sys_id_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload(
            {**_PAYLOAD, "batteries": [{**_BAT1, "sys_id": "1"}]}
        )
        assert coordinator.data is None

    async def test_battery_soc_out_of_range_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload({**_PAYLOAD, "batteries": [{**_BAT1, "soc": 101}]})
        assert coordinator.data is None

    async def test_well_formed_payload_is_accepted(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload(_PAYLOAD)
        assert coordinator.data is not None
        assert coordinator.data["voltage"] == 51.2

    async def test_non_object_cell_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """A cells entry that isn't an object must be dropped before it can
        reach is_cell_present/sensor.py, which call .get() on each cell and
        would otherwise raise AttributeError on a bare string/number."""
        coordinator._process_payload(
            {**_PAYLOAD, "batteries": [{**_BAT1, "cells": ["not-an-object"]}]}
        )
        assert coordinator.data is None

    async def test_cells_not_a_list_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload(
            {**_PAYLOAD, "batteries": [{**_BAT1, "cells": {"cell_id": 0}}]}
        )
        assert coordinator.data is None

    async def test_cell_missing_cell_id_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload(
            {**_PAYLOAD, "batteries": [{**_BAT1, "cells": [{"voltage": 3.4}]}]}
        )
        assert coordinator.data is None

    async def test_cell_non_int_cell_id_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        coordinator._process_payload(
            {**_PAYLOAD, "batteries": [{**_BAT1, "cells": [{"cell_id": "0"}]}]}
        )
        assert coordinator.data is None

    async def test_duplicate_cell_id_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        cell0 = {"cell_id": 0, "voltage": 3.4}
        cell0_dup = {"cell_id": 0, "voltage": 3.5}
        coordinator._process_payload(
            {**_PAYLOAD, "batteries": [{**_BAT1, "cells": [cell0, cell0_dup]}]}
        )
        assert coordinator.data is None

    async def test_too_many_cells_is_rejected(
        self, coordinator: PylontechCoordinator
    ) -> None:
        """Guards against unbounded PylontechCellSensor entity creation in
        sensor.py, which never removes entities once registered."""
        cells = [{"cell_id": i, "voltage": 3.4} for i in range(33)]
        coordinator._process_payload(
            {**_PAYLOAD, "batteries": [{**_BAT1, "cells": cells}]}
        )
        assert coordinator.data is None

    async def test_max_cells_is_accepted(
        self, coordinator: PylontechCoordinator
    ) -> None:
        cells = [{"cell_id": i, "voltage": 3.4} for i in range(32)]
        coordinator._process_payload(
            {**_PAYLOAD, "batteries": [{**_BAT1, "cells": cells}]}
        )
        assert coordinator.data is not None
