"""DataUpdateCoordinator for the Pylontech MQTT integration."""

import json
import logging

import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from paho.mqtt.enums import CallbackAPIVersion

from .const import DOMAIN
from .structs import PylontechBattery, PylontechSystem

_LOGGER = logging.getLogger(__name__)


class PylontechCoordinator(DataUpdateCoordinator[PylontechSystem]):
    """Receive Pylontech BMS data pushed via MQTT from the sidecar container."""

    def __init__(
        self,
        hass: HomeAssistant,
        mqtt_host: str,
        mqtt_port: int,
        mqtt_user: str,
        mqtt_pass: str,
        topic_prefix: str,
        default_capacity: float = 2.4,
    ) -> None:
        # No update_interval — data arrives via push, not polling.
        super().__init__(hass, _LOGGER, name=DOMAIN)

        self._mqtt_host = mqtt_host
        self._mqtt_port = mqtt_port
        self._mqtt_user = mqtt_user
        self._mqtt_pass = mqtt_pass
        self._state_topic = f"{topic_prefix}/state"
        self._avail_topic = f"{topic_prefix}/availability"
        self._client: mqtt.Client | None = None

        self.default_capacity = default_capacity
        self.battery_capacities: dict[int, float] = {}

        # Start unavailable until the first MQTT message arrives.
        self.last_update_success = False

    # ------------------------------------------------------------------
    # Setup / teardown  (called from executor thread via async_add_executor_job)
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
            self._client.loop_stop()
            self._client.disconnect()
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

    def _on_message(self, client, userdata, msg):
        if msg.topic == self._avail_topic:
            if msg.payload.decode() != "online" and self.last_update_success:
                self.hass.loop.call_soon_threadsafe(self._mark_unavailable)
            return

        try:
            payload = json.loads(msg.payload.decode())
            system = self._deserialize(payload)
            self._compute_energy_stored(system)
            # Schedule data update on the HA event loop (thread-safe).
            self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, system)
        except Exception as err:
            _LOGGER.error("Error processing MQTT message: %s", err, exc_info=True)

    # ------------------------------------------------------------------
    # DataUpdateCoordinator override
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> PylontechSystem:
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

    def _mark_unavailable(self) -> None:
        self.last_update_success = False
        self.async_update_listeners()

    def _deserialize(self, data: dict) -> PylontechSystem:
        """Build a PylontechSystem dataclass instance from a JSON payload dict."""
        batteries: list[PylontechBattery] = []
        for b in data.get("batteries", []):
            batteries.append(
                PylontechBattery(
                    sys_id=b.get("sys_id", 0),
                    voltage=b.get("voltage", 0),
                    current=b.get("current", 0),
                    temperature=b.get("temperature", 0),
                    soc=b.get("soc", 0),
                    status=b.get("status", ""),
                    power=b.get("power", 0),
                    raw=b.get("raw", ""),
                    energy_stored=0.0,
                    temp_low=b.get("temp_low"),
                    temp_high=b.get("temp_high"),
                    volt_low=b.get("volt_low"),
                    volt_high=b.get("volt_high"),
                    volt_status=b.get("volt_status"),
                    curr_status=b.get("curr_status"),
                    temp_status=b.get("temp_status"),
                    batt_volt_status=b.get("batt_volt_status"),
                    batt_temp_status=b.get("batt_temp_status"),
                )
            )

        return PylontechSystem(
            voltage=data.get("voltage", 0),
            current=data.get("current", 0),
            soc=data.get("soc", 0),
            power=data.get("power", 0),
            energy_in=data.get("energy_in", 0.0),
            energy_out=data.get("energy_out", 0.0),
            energy_stored=0.0,
            cell_count=data.get("cell_count"),
            spec=data.get("spec"),
            barcode=data.get("barcode"),
            fw_version=data.get("fw_version"),
            soft_version=data.get("soft_version"),
            board_version=data.get("board_version"),
            boot_version=data.get("boot_version"),
            comm_version=data.get("comm_version"),
            release_date=data.get("release_date"),
            manufacturer=data.get("manufacturer"),
            model=data.get("model"),
            max_charge_curr=data.get("max_charge_curr"),
            max_dischg_curr=data.get("max_dischg_curr"),
            bms_time=data.get("bms_time"),
            cycles=data.get("cycles"),
            soh=data.get("soh"),
            charge_times=data.get("charge_times"),
            discharge_cnt=data.get("discharge_cnt"),
            idle_times=data.get("idle_times"),
            shut_times=data.get("shut_times"),
            reset_times=data.get("reset_times"),
            sc_times=data.get("sc_times"),
            bat_ov_times=data.get("bat_ov_times"),
            bat_hv_times=data.get("bat_hv_times"),
            bat_lv_times=data.get("bat_lv_times"),
            bat_uv_times=data.get("bat_uv_times"),
            pwr_ov_times=data.get("pwr_ov_times"),
            pwr_hv_times=data.get("pwr_hv_times"),
            life_warn_times=data.get("life_warn_times"),
            life_alarm_times=data.get("life_alarm_times"),
            pwr_coulomb=data.get("pwr_coulomb"),
            dsg_cap=data.get("dsg_cap"),
            raw=data.get("raw", ""),
            batteries=batteries,
        )

    def _compute_energy_stored(self, system: PylontechSystem) -> None:
        """Compute energy_stored per battery and system total from SOC × capacity."""
        total = 0.0
        for bat in system.batteries:
            cap = self.battery_capacities.get(bat.sys_id, self.default_capacity)
            bat.energy_stored = round(cap * (bat.soc / 100.0), 3)
            total += bat.energy_stored
        system.energy_stored = round(total, 3)

    def set_battery_capacity(self, bat_id: int, capacity: float) -> None:
        """Update the configured capacity for a specific battery module."""
        self.battery_capacities[bat_id] = capacity
