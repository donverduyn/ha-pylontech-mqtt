"""DataUpdateCoordinator for the Pylontech MQTT integration."""

import json
import logging
import math
import time
from datetime import timedelta

import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from paho.mqtt.enums import CallbackAPIVersion

from .capacity import parse_spec_capacity
from .const import DEFAULT_BATTERY_CAPACITY, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Top-level fields every state payload must carry, and the range each is
# allowed to fall in (None on either side means unbounded that direction).
_REQUIRED_NUMERIC_FIELDS: dict[str, tuple[float | None, float | None]] = {
    "voltage": (0, None),
    "current": (None, None),
    "soc": (0, 100),
    "power": (None, None),
    "energy_in": (0, None),
    "energy_out": (0, None),
}
# Same idea, per battery entry in the "batteries" list.
_REQUIRED_BATTERY_NUMERIC_FIELDS: dict[str, tuple[float | None, float | None]] = {
    "voltage": (0, None),
    "current": (None, None),
    "soc": (0, 100),
    "power": (None, None),
}


def _validate_number(
    value: object, low: float | None, high: float | None
) -> str | None:
    """Return an error string if *value* isn't a finite number in [low, high]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"expected a number, got {value!r}"
    if not math.isfinite(value):
        return f"expected a finite number, got {value!r}"
    if low is not None and value < low:
        return f"{value!r} is below the minimum of {low!r}"
    if high is not None and value > high:
        return f"{value!r} is above the maximum of {high!r}"
    return None


def _validate_state_payload(payload: dict) -> str | None:
    """Return a description of the first problem found in *payload*, or None if valid.

    A publisher that is partial, incompatible, or simply buggy (e.g. an empty
    ``{}``) must never be allowed to overwrite good live data with
    zero-filled voltage/SOC/power/energy — this runs before any of that data
    is accepted, so a bad message is dropped and the last-known-good reading
    is kept instead.
    """
    for field, (low, high) in _REQUIRED_NUMERIC_FIELDS.items():
        if field not in payload:
            return f"missing required field {field!r}"
        error = _validate_number(payload[field], low, high)
        if error is not None:
            return f"field {field!r}: {error}"

    batteries = payload.get("batteries")
    if not isinstance(batteries, list):
        return "field 'batteries' must be a list"
    for i, bat in enumerate(batteries):
        if not isinstance(bat, dict):
            return f"batteries[{i}] must be an object"
        sys_id = bat.get("sys_id")
        if not isinstance(sys_id, int) or isinstance(sys_id, bool):
            return f"batteries[{i}].sys_id must be an int, got {sys_id!r}"
        for field, (low, high) in _REQUIRED_BATTERY_NUMERIC_FIELDS.items():
            if field not in bat:
                return f"batteries[{i}].{field!r} is missing"
            error = _validate_number(bat[field], low, high)
            if error is not None:
                return f"batteries[{i}].{field!r}: {error}"

    return None


class PylontechCoordinator(DataUpdateCoordinator[dict]):
    """Receive Pylontech BMS data pushed via MQTT from the sidecar container."""

    # A stuck sidecar poll loop stops publishing entirely (no more "online"
    # LWT, no more state messages) while its MQTT connection stays up, so
    # neither the LWT nor _on_disconnect ever fires to mark it unavailable.
    # This is a periodic backstop for that specific failure mode — a
    # multiple of any reasonable POLL_INTERVAL, generous enough to tolerate
    # normal broker hiccups without flapping availability.
    _STALE_TIMEOUT_SECONDS = 300
    _WATCHDOG_INTERVAL_SECONDS = 60

    def __init__(
        self,
        hass: HomeAssistant,
        mqtt_host: str,
        mqtt_port: int,
        mqtt_user: str,
        mqtt_pass: str,
        topic_prefix: str,
        stack_id: str,
        mqtt_tls: bool = False,
        default_capacity: float = DEFAULT_BATTERY_CAPACITY,
    ) -> None:
        # No update_interval — data arrives via push, not polling.
        super().__init__(hass, _LOGGER, name=DOMAIN)

        self._mqtt_host = mqtt_host
        self._mqtt_port = mqtt_port
        self._mqtt_user = mqtt_user
        self._mqtt_pass = mqtt_pass
        self._mqtt_tls = mqtt_tls
        self.topic_prefix = topic_prefix
        self.stack_id = stack_id
        self._state_topic = f"{topic_prefix}/state"
        self._avail_topic = f"{topic_prefix}/availability"
        self._client: mqtt.Client | None = None
        self._last_message_monotonic: float | None = None
        self._unsub_watchdog = None

        self.default_capacity = default_capacity
        self.battery_capacities: dict[int, float] = {}
        self._auto_capacity_set: bool = False

        # Start unavailable until the first MQTT message arrives.
        self.last_update_success = False

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Connect to the MQTT broker and subscribe to topics.

        Uses connect_async so HA startup is not blocked if the broker is
        temporarily unreachable.  paho-mqtt will keep retrying in background.
        """
        client = mqtt.Client(CallbackAPIVersion.VERSION2)
        if self._mqtt_user:
            client.username_pw_set(self._mqtt_user, self._mqtt_pass)
        if self._mqtt_tls:
            client.tls_set()
        client.reconnect_delay_set(min_delay=5, max_delay=120)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        # connect_async stores connection params without blocking; loop_start
        # establishes the connection in background and retries automatically.
        client.connect_async(self._mqtt_host, self._mqtt_port, 60)
        self._client = client
        client.loop_start()

        # setup() runs on the event loop (see the caller in __init__.py), so
        # this can be registered directly.
        self._unsub_watchdog = async_track_time_interval(
            self.hass,
            self._check_staleness,
            timedelta(seconds=self._WATCHDOG_INTERVAL_SECONDS),
        )

    def shutdown(self) -> None:
        """Disconnect from the MQTT broker."""
        if self._unsub_watchdog is not None:
            # shutdown() runs in an executor thread (see the caller in
            # __init__.py), but the unsub callback is event-loop-only.
            self.hass.loop.call_soon_threadsafe(self._unsub_watchdog)
            self._unsub_watchdog = None
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as err:
                _LOGGER.debug("Error during MQTT client shutdown: %s", err)
            self._client = None

    # ------------------------------------------------------------------
    # MQTT callbacks  (execute in paho's background thread)
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            _LOGGER.error("MQTT connect failed: %s", reason_code)
            return
        _LOGGER.info("MQTT connected to %s:%d", self._mqtt_host, self._mqtt_port)
        client.subscribe(self._state_topic)
        client.subscribe(self._avail_topic)

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties
    ):
        _LOGGER.warning("MQTT disconnected: %s", reason_code)
        # Mark unavailable immediately so HA entities reflect the loss of comms.
        # paho-mqtt will reconnect automatically; when it does, _on_connect
        # re-subscribes and the broker re-delivers the retained availability
        # payload, which will call _mark_available again if the sidecar is up.
        self.hass.loop.call_soon_threadsafe(self._mark_unavailable)

    def _on_message(self, client, userdata, msg):
        # Any message at all — state or availability — proves the sidecar's
        # poll loop is still alive and publishing; feeds the staleness
        # watchdog below.
        self._last_message_monotonic = time.monotonic()

        if msg.topic == self._avail_topic:
            if msg.payload.decode("utf-8", errors="replace") == "online":
                # Mark available immediately — do not gate on data being present.
                # The "online" availability message can arrive before the first
                # state payload; gating would cause the device to appear offline
                # until the race resolves.
                self.hass.loop.call_soon_threadsafe(self._mark_available)
            else:
                self.hass.loop.call_soon_threadsafe(self._mark_unavailable)
            return

        try:
            payload = json.loads(msg.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            _LOGGER.error("Error decoding MQTT message: %s", err)
            return

        # Hand off to the HA event loop — all state mutations (deserialization,
        # energy computation, capacity lookups) happen on a single thread so
        # battery_capacities never needs a lock.
        self.hass.loop.call_soon_threadsafe(self._process_payload, payload)

    def _process_payload(self, payload: dict) -> None:
        """Deserialize and update coordinator data. Always called on the HA event loop."""
        if not isinstance(payload, dict):
            _LOGGER.error(
                "Unexpected MQTT payload type '%s' — expected a JSON object; dropping",
                type(payload).__name__,
            )
            return
        error = _validate_state_payload(payload)
        if error is not None:
            _LOGGER.error("Rejecting malformed MQTT state payload — %s", error)
            return
        try:
            system = self._deserialize(payload)
            # On the first payload that includes a parseable spec string (e.g.
            # "48V/100AH"), auto-derive the per-module kWh capacity so battery
            # number entities are pre-filled on first discovery instead of
            # defaulting to the US2000 fallback.
            if not self._auto_capacity_set and system.get("spec"):
                try:
                    derived = parse_spec_capacity(system["spec"])
                except ValueError:
                    _LOGGER.debug("Could not parse battery spec '%s'", system["spec"])
                else:
                    if derived is not None:
                        self.default_capacity = derived
                        self._auto_capacity_set = True
                        _LOGGER.debug(
                            "Battery capacity auto-set to %.2f kWh from spec '%s'",
                            derived,
                            system["spec"],
                        )
            self._compute_energy_stored(system)
            self.async_set_updated_data(system)
        except Exception as err:
            _LOGGER.error("Error processing MQTT payload: %s", err, exc_info=True)

    # ------------------------------------------------------------------
    # DataUpdateCoordinator override
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict:
        """Re-compute derived values from last received data.

        Called when a manual refresh is requested (e.g. after battery
        capacity is changed in the number platform).
        """
        if self.data is None:
            raise UpdateFailed("No MQTT data received yet")
        self._compute_energy_stored(self.data)
        return self.data

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _mark_available(self) -> None:
        self.last_update_success = True
        self.async_update_listeners()

    def _mark_unavailable(self) -> None:
        self.last_update_success = False
        self.async_update_listeners()

    @callback
    def _check_staleness(self, now) -> None:
        """Mark unavailable if no MQTT message has arrived in a while.

        Runs on a timer rather than reacting to an event, because there is
        no event to react to: a hung sidecar poll loop keeps its MQTT
        connection open (so no disconnect/LWT fires) while simply never
        publishing again, and nothing else here would ever notice.
        """
        if not self.last_update_success or self._last_message_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_message_monotonic
        if elapsed > self._STALE_TIMEOUT_SECONDS:
            _LOGGER.warning(
                "No MQTT message received in %.0fs (threshold %ds) — "
                "marking unavailable",
                elapsed,
                self._STALE_TIMEOUT_SECONDS,
            )
            self._mark_unavailable()

    def _deserialize(self, data: dict) -> dict:
        """Normalise the JSON payload dict, injecting energy_stored defaults."""
        batteries = [
            {**b, "energy_stored": 0.0, "cells": b.get("cells", [])}
            for b in data.get("batteries", [])
        ]
        required_defaults = {
            "voltage": 0,
            "current": 0,
            "soc": 0,
            "power": 0,
            "energy_in": 0.0,
            "energy_out": 0.0,
        }
        return {
            **required_defaults,
            **data,
            "energy_stored": 0.0,
            "batteries": batteries,
        }

    def _compute_energy_stored(self, system: dict) -> None:
        """Compute energy_stored per battery and system total from SOC × capacity."""
        total = 0.0
        for bat in system["batteries"]:
            bat_id = bat.get("sys_id")
            if bat_id is None:
                continue
            cap = self.battery_capacities.get(bat_id, self.default_capacity)
            bat["energy_stored"] = round(cap * (bat["soc"] / 100.0), 3)
            total += bat["energy_stored"]
        system["energy_stored"] = round(total, 3)

    def set_battery_capacity(self, bat_id: int, capacity: float) -> None:
        """Update the configured capacity for a specific battery module."""
        self.battery_capacities[bat_id] = capacity

    # ------------------------------------------------------------------
    # Per-module / per-cell presence
    #
    # The sidecar's "pwr" parser drops "Absent" module rows from the
    # payload entirely (see pylontech_parser.parse_pwr), so a module that
    # drops out simply stops appearing in system["batteries"] on the next
    # message — it is never reported as present-but-errored. Entities for
    # that module and its cells are deliberately NOT removed from the
    # registry when this happens (that would delete history/customization
    # for what may just be a transient blip, and re-adding it later would
    # get a fresh entity_id, undermining the stable-identity fix in
    # entity.py). Instead, PylontechBatteryEntity/PylontechCellEntity use
    # these to report themselves unavailable while their module is absent,
    # rather than silently freezing on stale last-known values.
    # ------------------------------------------------------------------

    def is_battery_present(self, bat_id: int) -> bool:
        """Return whether *bat_id* appears in the most recent payload."""
        if not self.data:
            return False
        return any(
            bat.get("sys_id") == bat_id for bat in self.data.get("batteries", [])
        )

    def is_cell_present(self, bat_id: int, cell_id: int) -> bool:
        """Return whether *cell_id* of *bat_id* appears in the most recent payload."""
        if not self.data:
            return False
        for bat in self.data.get("batteries", []):
            if bat.get("sys_id") == bat_id:
                return any(
                    cell.get("cell_id") == cell_id for cell in bat.get("cells", [])
                )
        return False
