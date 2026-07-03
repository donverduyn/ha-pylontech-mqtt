"""DataUpdateCoordinator for the Pylontech MQTT integration."""

import json
import logging

import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from paho.mqtt.enums import CallbackAPIVersion

from .capacity import parse_spec_capacity
from .const import DEFAULT_BATTERY_CAPACITY, DOMAIN

_LOGGER = logging.getLogger(__name__)


class PylontechCoordinator(DataUpdateCoordinator[dict]):
    """Receive Pylontech BMS data pushed via MQTT from the sidecar container."""

    def __init__(
        self,
        hass: HomeAssistant,
        mqtt_host: str,
        mqtt_port: int,
        mqtt_user: str,
        mqtt_pass: str,
        topic_prefix: str,
        default_capacity: float = DEFAULT_BATTERY_CAPACITY,
    ) -> None:
        # No update_interval — data arrives via push, not polling.
        super().__init__(hass, _LOGGER, name=DOMAIN)

        self._mqtt_host = mqtt_host
        self._mqtt_port = mqtt_port
        self._mqtt_user = mqtt_user
        self._mqtt_pass = mqtt_pass
        self.topic_prefix = topic_prefix
        self._state_topic = f"{topic_prefix}/state"
        self._avail_topic = f"{topic_prefix}/availability"
        self._client: mqtt.Client | None = None

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
        client.reconnect_delay_set(min_delay=5, max_delay=120)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        # connect_async stores connection params without blocking; loop_start
        # establishes the connection in background and retries automatically.
        client.connect_async(self._mqtt_host, self._mqtt_port, 60)
        self._client = client
        client.loop_start()

    def shutdown(self) -> None:
        """Disconnect from the MQTT broker."""
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
