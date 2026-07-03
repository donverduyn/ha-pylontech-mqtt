#!/usr/bin/env python3
"""
pylon2mqtt — Pylontech BMS to MQTT bridge.

All configuration is via environment variables:

  CONNECTION_TYPE   : "serial" (default) or "tcp"
  SERIAL_PORT       : serial device path (serial mode), default /dev/ttyUSB0
  BAUD_RATE         : baud rate (serial mode), default 115200
  TCP_HOST          : hostname or IP (tcp mode)
  TCP_PORT          : port (tcp mode), default 23

  MQTT_BROKER       : MQTT broker host (required)
  MQTT_PORT         : MQTT broker port, default 1883
  MQTT_USER         : MQTT username (optional)
  MQTT_PASS         : MQTT password (optional)
  MQTT_TOPIC_PREFIX : base MQTT topic, default "pylontech/stack"

  POLL_INTERVAL     : seconds between polls, default 15
  AUTO_SYNC_TIME    : "true" to sync BMS clock on startup, default "false"
"""

import json
import logging
import os
import socket
import sys
import time
from dataclasses import asdict
from datetime import datetime
from typing import Optional

import paho.mqtt.client as mqtt
import serial
from paho.mqtt.enums import CallbackAPIVersion
from pylon_parser import PylontechParser
from structs import PylontechSystem

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
_LOGGER = logging.getLogger("pylon2mqtt")


def _int_env(name: str, default: int) -> int:
    """Parse an integer environment variable; log and fall back to default on error."""
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError:
        _LOGGER.error(
            "Invalid value for %s=%r (expected integer) — using default %d",
            name,
            raw,
            default,
        )
        return default


# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------

CONNECTION_TYPE = os.getenv("CONNECTION_TYPE", "serial").lower()
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
BAUD_RATE = _int_env("BAUD_RATE", 115200)
TCP_HOST = os.getenv("TCP_HOST", "")
TCP_PORT = _int_env("TCP_PORT", 23)

MQTT_BROKER = os.getenv("MQTT_BROKER", "")
MQTT_PORT_ENV = _int_env("MQTT_PORT", 1883)
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "pylontech/stack")

POLL_INTERVAL = _int_env("POLL_INTERVAL", 15)
AUTO_SYNC_TIME = os.getenv("AUTO_SYNC_TIME", "false").lower() == "true"

STATE_TOPIC = f"{MQTT_TOPIC_PREFIX}/state"
AVAIL_TOPIC = f"{MQTT_TOPIC_PREFIX}/availability"

# ---------------------------------------------------------------------------
# BMS connection
# ---------------------------------------------------------------------------


class BmsConnection:
    """Manages a serial or TCP connection to the Pylontech BMS."""

    def __init__(self) -> None:
        self._tcp: socket.socket | None = None
        self._serial: serial.Serial | None = None

    def _open(self) -> None:
        if CONNECTION_TYPE == "tcp":
            _LOGGER.info("Opening TCP connection to %s:%d", TCP_HOST, TCP_PORT)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((TCP_HOST, TCP_PORT))
            self._tcp = s
        else:
            _LOGGER.info("Opening serial on %s @ %d baud", SERIAL_PORT, BAUD_RATE)
            self._serial = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2)

    def _ensure_open(self) -> None:
        if CONNECTION_TYPE == "tcp":
            if self._tcp is None:
                self._open()
        else:
            if self._serial is None:
                self._open()
            elif not self._serial.is_open:
                self._serial.open()

    def send_command(self, cmd: str) -> str:
        """Send a command and return the ASCII response."""
        self._ensure_open()
        if CONNECTION_TYPE == "tcp":
            assert self._tcp is not None
            self._tcp.sendall((cmd + "\n").encode("ascii"))
            time.sleep(1.0)
            data = b""
            self._tcp.settimeout(2.0)
            try:
                while True:
                    chunk = self._tcp.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
            return data.decode("ascii", errors="ignore")
        else:
            assert self._serial is not None
            self._serial.reset_input_buffer()
            self._serial.write(b"\n")
            time.sleep(0.1)
            self._serial.read_all()
            self._serial.write((cmd + "\n").encode("ascii"))
            time.sleep(1.0)
            return (self._serial.read_all() or b"").decode("ascii", errors="ignore")

    def close(self) -> None:
        for conn in (self._tcp, self._serial):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self._tcp = None
        self._serial = None


# ---------------------------------------------------------------------------
# Energy tracker
# ---------------------------------------------------------------------------


class EnergyTracker:
    def __init__(self) -> None:
        self.energy_in: float = 0.0
        self.energy_out: float = 0.0
        self._last_time: Optional[datetime] = None

    def update(self, power_w: float) -> None:
        now = datetime.now()
        if self._last_time is not None:
            hours = (now - self._last_time).total_seconds() / 3600.0
            kwh = abs(power_w * hours) / 1000.0
            if power_w >= 0:
                self.energy_in += kwh
            else:
                self.energy_out += kwh
        self._last_time = now


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if not MQTT_BROKER:
        _LOGGER.error("MQTT_BROKER environment variable is required")
        sys.exit(1)
    if CONNECTION_TYPE == "tcp" and not TCP_HOST:
        _LOGGER.error("TCP_HOST is required when CONNECTION_TYPE=tcp")
        sys.exit(1)

    _LOGGER.info(
        "Starting pylon2mqtt | connection=%s | MQTT=%s:%d | topic=%s | poll=%ds",
        CONNECTION_TYPE.upper(),
        MQTT_BROKER,
        MQTT_PORT_ENV,
        MQTT_TOPIC_PREFIX,
        POLL_INTERVAL,
    )

    # -- MQTT setup --
    client = mqtt.Client(CallbackAPIVersion.VERSION2)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(AVAIL_TOPIC, "offline", retain=True)

    def on_connect(c, userdata, flags, reason_code, properties):  # noqa: ANN001
        if reason_code.is_failure:
            _LOGGER.error("MQTT connect failed: %s", reason_code)
        else:
            _LOGGER.info("MQTT connected")
            c.publish(AVAIL_TOPIC, "online", retain=True)

    def on_disconnect(c, userdata, disconnect_flags, reason_code, properties):  # noqa: ANN001
        _LOGGER.warning("MQTT disconnected: %s", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT_ENV, 60)
            break
        except Exception as err:
            _LOGGER.error("Cannot connect to MQTT broker: %s — retrying in 10 s", err)
            time.sleep(10)

    client.loop_start()

    # -- BMS poll loop --
    bms = BmsConnection()
    energy = EnergyTracker()
    system: Optional[PylontechSystem] = None
    info_fetched = False

    while True:
        try:
            if not info_fetched:
                _LOGGER.info("Fetching device info...")
                raw_info = bms.send_command("info")
                if system is None:
                    system = PylontechSystem(0, 0, 0, 0, 0.0, 0.0, 0.0)
                PylontechParser.parse_info(raw_info, system)
                info_fetched = True

                if AUTO_SYNC_TIME:
                    _LOGGER.info("Syncing BMS time...")
                    bms.send_command(
                        PylontechParser.generate_time_command(datetime.now())
                    )

            _LOGGER.debug("Polling BMS...")
            raw_pwr = bms.send_command("pwr")
            if "Power Volt" not in raw_pwr:
                time.sleep(1.0)
                raw_pwr = bms.send_command("pwr")
            if "Power Volt" not in raw_pwr:
                raise IOError("Did not receive valid 'pwr' response")

            raw_stat = bms.send_command("stat")
            raw_time = bms.send_command("time")

            if system is None:
                system = PylontechSystem(0, 0, 0, 0, 0.0, 0.0, 0.0)

            PylontechParser.parse_pwr(raw_pwr, system)

            for bat in system.batteries:
                try:
                    raw_bat = bms.send_command(f"bat {bat.sys_id}")
                    PylontechParser.parse_bat(raw_bat, bat)
                except Exception as bat_err:
                    _LOGGER.warning(
                        "Could not fetch cell data for battery %d: %s",
                        bat.sys_id,
                        bat_err,
                    )

            PylontechParser.parse_stat(raw_stat, system)
            PylontechParser.parse_time(raw_time, system)

            energy.update(system.power)
            system.energy_in = round(energy.energy_in, 3)
            system.energy_out = round(energy.energy_out, 3)

            payload = json.dumps(asdict(system), default=str)
            client.publish(STATE_TOPIC, payload, retain=True)
            _LOGGER.info(
                "Published | V=%.2fV I=%.2fA SOC=%.1f%% P=%.1fW batteries=%d cells=%d",
                system.voltage,
                system.current,
                system.soc,
                system.power,
                len(system.batteries),
                sum(len(b.cells) for b in system.batteries),
            )

        except (serial.SerialException, OSError, IOError) as err:
            _LOGGER.error("BMS connection error: %s — reconnecting in 5 s", err)
            bms.close()
            info_fetched = False
            time.sleep(5)
            continue
        except KeyboardInterrupt:
            _LOGGER.info("Shutting down...")
            client.publish(AVAIL_TOPIC, "offline", retain=True)
            client.loop_stop()
            client.disconnect()
            bms.close()
            sys.exit(0)
        except Exception as err:
            _LOGGER.error("Unexpected error: %s", err, exc_info=True)
            info_fetched = False

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
