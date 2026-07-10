"""DataUpdateCoordinator for the Pylontech MQTT integration."""

import json
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Any, cast

import paho.mqtt.client as mqtt
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from paho.mqtt.client import ConnectFlags, DisconnectFlags, MQTTMessage
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from .capacity import parse_spec_capacity
from .const import DEFAULT_BATTERY_CAPACITY, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Bumped in lockstep with the sidecar's own SCHEMA_VERSION (src/main.py)
# whenever the state payload's shape changes in a way this integration needs
# to know about. The sidecar and integration are installed independently (one
# via Docker, one via HACS), so there is no other way to detect a mismatched
# pair before it silently produces broken sensors.
_SCHEMA_VERSION = 1

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
# Real Pylontech modules top out around 15-16 cells; this caps well above
# that (rather than matching it exactly) so real hardware is never rejected,
# while still bounding how many PylontechCellSensor entities a single
# battery's "cells" list can cause sensor.py to create — those entities are
# never removed once registered (see the per-module/per-cell presence
# comment below), so an unbounded or buggy publisher would otherwise leave
# permanent junk entities behind.
_MAX_CELLS_PER_BATTERY = 32


def find_battery(data: dict[str, Any] | None, bat_id: int) -> dict[str, Any] | None:
    """Return the battery entry with sys_id == bat_id from a state payload, or None."""
    if not data:
        return None
    for bat in data.get("batteries", []):
        if bat.get("sys_id") == bat_id:
            return cast(dict[str, Any], bat)
    return None


def find_cell(battery: dict[str, Any] | None, cell_id: int) -> dict[str, Any] | None:
    """Return the cell entry with cell_id == cell_id from a battery entry, or None."""
    if not battery:
        return None
    for cell in battery.get("cells", []):
        if cell.get("cell_id") == cell_id:
            return cast(dict[str, Any], cell)
    return None


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


def _validate_state_payload(payload: dict[str, Any]) -> str | None:
    """Return a description of the first problem found in *payload*, or None if valid.

    A publisher that is partial, incompatible, or simply buggy (e.g. an empty
    ``{}``) must never be allowed to overwrite good live data with
    zero-filled voltage/SOC/power/energy — this runs before any of that data
    is accepted, so a bad message is dropped and the last-known-good reading
    is kept instead.
    """
    schema_version = payload.get("schema_version")
    if schema_version != _SCHEMA_VERSION:
        return (
            f"schema_version {schema_version!r} is not supported by this "
            f"integration (expected {_SCHEMA_VERSION}) — update the sidecar "
            "container and the HA integration to matching releases"
        )

    for field, (low, high) in _REQUIRED_NUMERIC_FIELDS.items():
        if field not in payload:
            return f"missing required field {field!r}"
        error = _validate_number(payload[field], low, high)
        if error is not None:
            return f"field {field!r}: {error}"

    batteries = payload.get("batteries")
    if not isinstance(batteries, list):
        return "field 'batteries' must be a list"
    # isinstance narrowing on an Any-typed value produces list[Unknown] under
    # pyright (it still doesn't know the element type) but list[Any] under
    # mypy (which considers a second cast to that same type redundant) — cast
    # to the more specific element type both checkers agree is unresolved,
    # satisfying pyright without mypy flagging it as a no-op.
    batteries = cast(list[object], batteries)
    for i, bat in enumerate(batteries):
        if not isinstance(bat, dict):
            return f"batteries[{i}] must be an object"
        bat = cast(dict[str, Any], bat)
        sys_id = bat.get("sys_id")
        if not isinstance(sys_id, int) or isinstance(sys_id, bool):
            return f"batteries[{i}].sys_id must be an int, got {sys_id!r}"
        for field, (low, high) in _REQUIRED_BATTERY_NUMERIC_FIELDS.items():
            if field not in bat:
                return f"batteries[{i}].{field!r} is missing"
            error = _validate_number(bat[field], low, high)
            if error is not None:
                return f"batteries[{i}].{field!r}: {error}"

        if "cells" in bat:
            cells = bat["cells"]
            if not isinstance(cells, list):
                return f"batteries[{i}].cells must be a list"
            cells = cast(list[object], cells)
            if len(cells) > _MAX_CELLS_PER_BATTERY:
                return (
                    f"batteries[{i}].cells has {len(cells)} entries, "
                    f"exceeding the maximum of {_MAX_CELLS_PER_BATTERY}"
                )
            seen_cell_ids: set[int] = set()
            for j, cell in enumerate(cells):
                if not isinstance(cell, dict):
                    return f"batteries[{i}].cells[{j}] must be an object"
                cell = cast(dict[str, Any], cell)
                cell_id = cell.get("cell_id")
                if not isinstance(cell_id, int) or isinstance(cell_id, bool):
                    return (
                        f"batteries[{i}].cells[{j}].cell_id must be an int, "
                        f"got {cell_id!r}"
                    )
                if cell_id in seen_cell_ids:
                    return (
                        f"batteries[{i}].cells[{j}].cell_id {cell_id!r} "
                        "is a duplicate within this battery"
                    )
                seen_cell_ids.add(cell_id)

    return None


class PylontechCoordinator(DataUpdateCoordinator[dict[str, Any]]):
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
        self._unsub_watchdog: CALLBACK_TYPE | None = None

        self.default_capacity = default_capacity
        self.battery_capacities: dict[int, float] = {}
        self._auto_capacity_set: bool = False

        # Start unavailable until the first MQTT message arrives.
        self.last_update_success = False

    # Setup / teardown

    def setup(self) -> None:
        """Connect to the MQTT broker and subscribe to topics.

        Uses connect_async so HA startup is not blocked if the broker is
        temporarily unreachable.  paho-mqtt will keep retrying in background.
        """
        client = mqtt.Client(CallbackAPIVersion.VERSION2)
        if self._mqtt_user:
            client.username_pw_set(self._mqtt_user, self._mqtt_pass)
        if self._mqtt_tls:
            # paho-mqtt imports `ssl` only under `if TYPE_CHECKING:`, and
            # pyright can't resolve ssl.VerifyMode from there in this
            # version, making tls_set's own inferred type partially unknown
            # regardless of the (argument-less) call here.
            client.tls_set()  # pyright: ignore[reportUnknownMemberType]
        client.reconnect_delay_set(min_delay=5, max_delay=120)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        # connect_async stores connection params without blocking; loop_start
        # establishes the connection in background and retries automatically.
        client.connect_async(self._mqtt_host, self._mqtt_port, 60)
        self._client = client
        client.loop_start()

        # Start the staleness clock now rather than leaving it unset until
        # the first valid state message: a sidecar that connects and reports
        # "online" but never manages to publish a single valid state payload
        # (e.g. a schema_version mismatch) must still trip the watchdog after
        # _STALE_TIMEOUT_SECONDS, instead of sitting "available" forever with
        # no data because the clock never started.
        self._last_message_monotonic = time.monotonic()

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

    # MQTT callbacks  (execute in paho's background thread)

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            _LOGGER.error("MQTT connect failed: %s", reason_code)
            return
        _LOGGER.info("MQTT connected to %s:%d", self._mqtt_host, self._mqtt_port)
        client.subscribe(self._state_topic)
        client.subscribe(self._avail_topic)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        _LOGGER.warning("MQTT disconnected: %s", reason_code)
        # Mark unavailable immediately so HA entities reflect the loss of comms.
        # paho-mqtt will reconnect automatically; when it does, _on_connect
        # re-subscribes and the broker re-delivers the retained availability
        # payload, which will call _mark_available again if the sidecar is up.
        self.hass.loop.call_soon_threadsafe(self._mark_unavailable)

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: MQTTMessage) -> None:
        if msg.topic == self._avail_topic:
            # Deliberately does NOT feed _last_message_monotonic: the sidecar
            # republishes "online" on every poll cycle regardless of whether
            # that cycle's state payload was valid, so treating it as a
            # freshness signal would let a chronically incompatible or
            # malformed publisher dodge the staleness watchdog forever while
            # never actually delivering usable data. Only a successfully
            # validated state payload (_process_payload) counts as freshness.
            if msg.payload.decode("utf-8", errors="replace") == "online":
                # Mark available immediately — do not gate on data being present.
                # The "online" availability message can arrive before the first
                # state payload; gating would cause the device to appear offline
                # until the race resolves. But once the staleness watchdog has
                # already judged the last valid payload too old, this periodic
                # republish (main.py sends "online" every poll cycle regardless
                # of that cycle's own payload validity) must not silently
                # restore availability on its own — only a fresh, successfully
                # validated state payload (which sets last_update_success via
                # async_set_updated_data in _process_payload) should be able to
                # clear a staleness-driven unavailable.
                stale = (
                    self._last_message_monotonic is not None
                    and time.monotonic() - self._last_message_monotonic
                    > self._STALE_TIMEOUT_SECONDS
                )
                if not stale:
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

    def _process_payload(self, payload: object) -> None:
        """Deserialize and update coordinator data. Always runs on the HA event loop."""
        if not isinstance(payload, dict):
            _LOGGER.error(
                "Unexpected MQTT payload type '%s' — expected a JSON object; dropping",
                type(payload).__name__,
            )
            return
        # Same Any-narrowing gap as in _validate_state_payload: isinstance
        # only recovers dict[Unknown, Unknown] here, not dict[str, Any].
        payload = cast(dict[str, Any], payload)
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
            self._last_message_monotonic = time.monotonic()
        except Exception as err:
            _LOGGER.error("Error processing MQTT payload: %s", err, exc_info=True)

    # DataUpdateCoordinator override

    async def _async_update_data(self) -> dict[str, Any]:
        """Re-compute derived values from last received data.

        Called when a manual refresh is requested (e.g. after battery
        capacity is changed in the number platform).
        """
        # HA's own DataUpdateCoordinator types self.data as _DataT (not
        # _DataT | None), even though it's None until the first update — see
        # its `self.data: _DataT = None  # type: ignore[assignment]`. This
        # check is real at runtime; pyright just can't see it.
        if self.data is None:  # pyright: ignore[reportUnnecessaryComparison]
            raise UpdateFailed("No MQTT data received yet")
        self._compute_energy_stored(self.data)
        return self.data

    # Helpers

    def _mark_available(self) -> None:
        self.last_update_success = True
        self.async_update_listeners()

    def _mark_unavailable(self) -> None:
        self.last_update_success = False
        self.async_update_listeners()

    @callback
    def _check_staleness(self, now: datetime) -> None:
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

    def _deserialize(self, data: dict[str, Any]) -> dict[str, Any]:
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

    def _compute_energy_stored(self, system: dict[str, Any]) -> None:
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

    # Per-module / per-cell presence
    #
    # The sidecar's "pwr" parser drops "Absent" module rows from the
    # payload entirely (see src/parser_schema.py's PWR_TABLE_SCHEMA
    # skip_row), so a module that
    # drops out simply stops appearing in system["batteries"] on the next
    # message — it is never reported as present-but-errored. Entities for
    # that module and its cells are deliberately NOT removed from the
    # registry when this happens (that would delete history/customization
    # for what may just be a transient blip, and re-adding it later would
    # get a fresh entity_id, undermining the stable-identity fix in
    # entity.py). Instead, PylontechBatteryEntity/PylontechCellEntity use
    # these to report themselves unavailable while their module is absent,
    # rather than silently freezing on stale last-known values.

    def is_battery_present(self, bat_id: int) -> bool:
        """Return whether *bat_id* appears in the most recent payload."""
        return find_battery(self.data, bat_id) is not None

    def is_cell_present(self, bat_id: int, cell_id: int) -> bool:
        """Return whether *cell_id* of *bat_id* appears in the most recent payload."""
        return find_cell(find_battery(self.data, bat_id), cell_id) is not None
