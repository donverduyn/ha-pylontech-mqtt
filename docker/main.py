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
import signal
import socket
import sys
import time
from dataclasses import asdict
from datetime import datetime
from typing import Optional

import paho.mqtt.client as mqtt
import serial
from paho.mqtt.enums import CallbackAPIVersion
from pylontech_parser import PylontechParser
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

# Path where the energy counters are persisted across container restarts.
# Override with the ENERGY_STATE_FILE env var (set to "" to disable persistence).
ENERGY_STATE_FILE = os.getenv("ENERGY_STATE_FILE", "/data/energy_state.json")

STATE_TOPIC = f"{MQTT_TOPIC_PREFIX}/state"
AVAIL_TOPIC = f"{MQTT_TOPIC_PREFIX}/availability"

# ---------------------------------------------------------------------------
# BMS connection
# ---------------------------------------------------------------------------


PROMPT = b"pylon>"
_READ_TIMEOUT = 5.0  # overall deadline waiting for a complete response
_POLL_INTERVAL = 0.1  # per-read timeout while polling for more data


class BmsConnection:
    """Manages a serial or TCP connection to the Pylontech BMS.

    Every command's response ends with the console's "pylon>" prompt, so
    reads poll in short bursts until that terminator appears (or a deadline
    elapses) instead of sleeping a fixed duration and grabbing whatever
    happens to be available — a fast response returns immediately, a slow
    one gets the full deadline, and a response is never returned truncated
    mid-stream.
    """

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
            # The console sends an unsolicited banner/prompt as soon as a
            # client connects; drain it now so it can't be mistaken for part
            # of the first real command's response.
            self._read_until_prompt()
        else:
            _LOGGER.info("Opening serial on %s @ %d baud", SERIAL_PORT, BAUD_RATE)
            self._serial = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=_POLL_INTERVAL)
            # Unlike a fresh TCP session, opening the serial port doesn't by
            # itself make the BMS print anything. Prime it with a blank line
            # to elicit a known-fresh prompt, then drain it — this also
            # clears out any stale bytes left over from a previous run.
            self._serial.reset_input_buffer()
            self._serial.write(b"\n")
            self._read_until_prompt()

    def _ensure_open(self) -> None:
        if CONNECTION_TYPE == "tcp":
            if self._tcp is None:
                self._open()
        else:
            if self._serial is None:
                self._open()
            elif not self._serial.is_open:
                self._serial.open()

    def _read_until_prompt(self) -> bytes:
        """Read chunks until the "pylon>" prompt appears, or _READ_TIMEOUT elapses."""
        deadline = time.monotonic() + _READ_TIMEOUT
        data = b""
        while time.monotonic() < deadline:
            if CONNECTION_TYPE == "tcp":
                if self._tcp is None:
                    break
                self._tcp.settimeout(_POLL_INTERVAL)
                try:
                    chunk = self._tcp.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break  # remote closed the connection
            else:
                if self._serial is None:
                    break
                chunk = self._serial.read(4096)
                if not chunk:
                    continue
            data += chunk
            if PROMPT in data:
                break
        return data

    def send_command(self, cmd: str) -> str:
        """Send a command and return the ASCII response."""
        self._ensure_open()
        if CONNECTION_TYPE == "tcp":
            if self._tcp is None:
                raise RuntimeError("TCP socket is not open")
            self._tcp.sendall((cmd + "\n").encode("ascii"))
        else:
            if self._serial is None:
                raise RuntimeError("Serial port is not open")
            self._serial.reset_input_buffer()
            self._serial.write((cmd + "\n").encode("ascii"))
        return self._read_until_prompt().decode("ascii", errors="ignore")

    def close(self) -> None:
        for conn in (self._tcp, self._serial):
            if conn is not None:
                try:
                    conn.close()
                except Exception as err:
                    _LOGGER.debug("Error closing BMS connection: %s", err)
        self._tcp = None
        self._serial = None


# ---------------------------------------------------------------------------
# Energy tracker
# ---------------------------------------------------------------------------


class EnergyTracker:
    # A gap longer than this is treated as a comms outage / stall rather than
    # a real interval — its energy is dropped instead of integrated, the same
    # way invalidate_last_time() already handles an explicit reconnect gap.
    # Guards against system clock jumps and long stalls that weren't
    # explicitly invalidated inflating the counters via a huge bogus delta.
    _MAX_INTERVAL_SECONDS = 3600.0

    def __init__(self, state_file: str = "") -> None:
        self.energy_in: float = 0.0
        self.energy_out: float = 0.0
        self._last_time: Optional[float] = None  # time.monotonic() timestamp
        self._last_power: Optional[float] = None
        self._state_file = state_file
        if state_file:
            self._load()

    def _load(self) -> None:
        """Restore counters from the state file; silently start from 0 on any error."""
        try:
            with open(self._state_file) as f:
                data = json.load(f)
            # Parse both values before assigning so a partial/corrupt file
            # (e.g. missing one key) leaves both counters at 0.
            energy_in = float(data["energy_in"])
            energy_out = float(data["energy_out"])
            self.energy_in = energy_in
            self.energy_out = energy_out
            _LOGGER.info(
                "Energy state restored: in=%.3f kWh out=%.3f kWh",
                self.energy_in,
                self.energy_out,
            )
        except FileNotFoundError:
            pass  # First run — no state file yet
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as err:
            _LOGGER.warning(
                "Could not load energy state from %s: %s", self._state_file, err
            )

    def _save(self) -> None:
        """Persist current counters to the state file.

        Writes to a temp file in the same directory and atomically renames it
        into place with os.replace(), so a crash or power loss mid-write can
        never leave a partially-written, corrupt state file — a reader always
        sees either the old or the new content in full.
        """
        try:
            parent = os.path.dirname(self._state_file) or "."
            os.makedirs(parent, exist_ok=True)
            tmp_path = f"{self._state_file}.tmp"
            with open(tmp_path, "w") as f:
                json.dump(
                    {"energy_in": self.energy_in, "energy_out": self.energy_out}, f
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._state_file)
        except OSError as err:
            _LOGGER.warning(
                "Could not save energy state to %s: %s", self._state_file, err
            )

    def update(self, power_w: float) -> None:
        now = time.monotonic()
        if self._last_time is not None and self._last_power is not None:
            elapsed_seconds = now - self._last_time
            if 0 < elapsed_seconds <= self._MAX_INTERVAL_SECONDS:
                # Trapezoidal integration: average this sample with the
                # previous one rather than assuming the whole interval was at
                # the latest reading (rectangular/step integration mis-counts
                # whenever power changes materially between polls).
                avg_power = (self._last_power + power_w) / 2.0
                hours = elapsed_seconds / 3600.0
                kwh = abs(avg_power * hours) / 1000.0
                if avg_power >= 0:
                    self.energy_in += kwh
                else:
                    self.energy_out += kwh
                if self._state_file:
                    self._save()
            elif elapsed_seconds > self._MAX_INTERVAL_SECONDS:
                _LOGGER.warning(
                    "Skipping energy integration over an unexpectedly long "
                    "%.0f s gap (system clock jump or stall?)",
                    elapsed_seconds,
                )
        self._last_time = now
        self._last_power = power_w

    def invalidate_last_time(self) -> None:
        """Clear the last-poll timestamp and power sample.

        Must be called after any communication gap (BMS error, reconnect) so
        that the next update() call does not compute a kWh delta spanning the
        outage period and falsely inflate the energy counters.
        """
        self._last_time = None
        self._last_power = None


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
    if CONNECTION_TYPE == "serial" and not SERIAL_PORT:
        _LOGGER.error("SERIAL_PORT must not be empty")
        sys.exit(1)
    if not (1 <= MQTT_PORT_ENV <= 65535):
        _LOGGER.error("MQTT_PORT must be between 1 and 65535 (got %d)", MQTT_PORT_ENV)
        sys.exit(1)
    if BAUD_RATE <= 0:
        _LOGGER.error("BAUD_RATE must be a positive integer (got %d)", BAUD_RATE)
        sys.exit(1)
    if POLL_INTERVAL <= 0:
        _LOGGER.error(
            "POLL_INTERVAL must be a positive integer (got %d)", POLL_INTERVAL
        )
        sys.exit(1)

    # Treat SIGTERM (sent by `docker stop`) the same as KeyboardInterrupt so the
    # clean-shutdown path publishes "offline" before exiting.
    def _handle_sigterm(signum, frame):  # noqa: ANN001
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)

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

    def on_disconnect(c, userdata, disconnect_flags, reason_code, properties):  # noqa: ANN001
        _LOGGER.warning("MQTT disconnected: %s", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    _retry_delay = 5
    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT_ENV, 60)
            break
        except Exception as err:
            _LOGGER.error(
                "Cannot connect to MQTT broker: %s — retrying in %d s",
                err,
                _retry_delay,
            )
            time.sleep(_retry_delay)
            _retry_delay = min(_retry_delay * 2, 120)

    client.loop_start()

    # -- BMS poll loop --
    bms = BmsConnection()
    energy = EnergyTracker(state_file=ENERGY_STATE_FILE)
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
            client.publish(AVAIL_TOPIC, "online", retain=True)
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
            client.publish(AVAIL_TOPIC, "offline", retain=True)
            bms.close()
            energy.invalidate_last_time()
            info_fetched = False
            time.sleep(5)
            continue
        except KeyboardInterrupt:
            _LOGGER.info("Shutting down...")
            msg = client.publish(AVAIL_TOPIC, "offline", retain=True)
            msg.wait_for_publish(timeout=5)
            client.loop_stop()
            client.disconnect()
            bms.close()
            sys.exit(0)
        except Exception as err:
            _LOGGER.error("Unexpected error: %s", err, exc_info=True)
            client.publish(AVAIL_TOPIC, "offline", retain=True)
            # Close the BMS connection so the next poll starts fresh; the
            # exception may have left the serial/TCP socket in a broken state.
            bms.close()
            energy.invalidate_last_time()
            info_fetched = False

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
