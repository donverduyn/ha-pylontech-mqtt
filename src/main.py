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
  MQTT_TLS          : "true" to connect over TLS, default "false"

  POLL_INTERVAL     : seconds between polls, default 15, max 150 (must stay
                      well under the HA integration's 300s staleness
                      watchdog)
  AUTO_SYNC_TIME    : "true" to sync BMS clock on startup, default "false"
  MONITORING_LEVEL  : "low", "medium" (default), or "high" — how much detail
                      to walk per battery on top of the aggregate "pwr" table:
                        low    - aggregate "pwr" only
                        medium - adds per-battery "pwr N" detail (events/fault
                                 status the aggregate table doesn't expose)
                        high   - adds per-cell "bat N" polling on top of
                                 medium — creates a further 9 HA entities per
                                 cell, so opt in deliberately on large stacks
  MAX_BATTERIES     : upper bound on "pwr N" probes when the aggregate "pwr"
                      response is rejected, default 16
"""

import json
import logging
import math
import os
import signal
import socket
import sys
import time
from dataclasses import asdict
from datetime import datetime
from types import FrameType
from typing import Any, Protocol

import paho.mqtt.client as mqtt
import serial
from paho.mqtt.client import ConnectFlags, DisconnectFlags
from paho.mqtt.enums import CallbackAPIVersion, MQTTErrorCode
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from parser import Parser, has_header
from parser_schema import (
    BAT_TABLE_SCHEMA,
    CMD_INFO,
    CMD_PWR,
    CMD_STAT,
    CMD_TIME,
    CONSOLE_ALT_TERMINATOR,
    CONSOLE_PROMPT,
    INFO_SCHEMA,
    PWR_INDEXED_SCHEMA,
    PWR_TABLE_SCHEMA,
    STAT_FIELDS,
    TIME_FIELDS,
    cmd_bat,
    cmd_pwr_indexed,
    generate_time_command,
)
from structs import PylontechBattery, PylontechSystem

# One Parser per BMS command, bound to its schema (src/parser_schema.py)
# at import time — src/parser.py's engine has no Pylontech-specific knowledge of
# its own, so this is the one place a schema and the engine that walks it meet.
_pwr_parser = Parser(PWR_TABLE_SCHEMA)
_pwr_indexed_parser = Parser(PWR_INDEXED_SCHEMA)
_info_parser = Parser(INFO_SCHEMA)
_stat_parser = Parser(STAT_FIELDS)
_time_parser = Parser(TIME_FIELDS)
_bat_parser = Parser(BAT_TABLE_SCHEMA)

# Logging

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


def _invalid_topic_prefix(topic: str) -> bool:
    """Return True if *topic* would break MQTT subscribe/publish at runtime.

    Mirrors custom_components/pylontech_mqtt/config_flow.py's
    _invalid_topic_prefix — the HA config flow rejects these same shapes, but
    a MQTT_TOPIC_PREFIX set directly on the sidecar (Docker env var) never
    goes through that form, so it must be checked again here rather than
    failing deep inside paho-mqtt at publish time.
    """
    return (
        not topic
        or topic != topic.strip()
        or "#" in topic
        or "+" in topic
        or topic.startswith("/")
        or topic.endswith("/")
    )


# Configuration from environment variables

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
MQTT_TLS = os.getenv("MQTT_TLS", "false").lower() == "true"

POLL_INTERVAL = _int_env("POLL_INTERVAL", 15)
# The Home Assistant integration's coordinator (custom_components/
# pylontech_mqtt/coordinator.py: _STALE_TIMEOUT_SECONDS) marks the device
# unavailable after 300s without an MQTT message. Capping POLL_INTERVAL at
# half that guarantees at least one missed poll's worth of margin before
# that watchdog would ever fire on a healthy, correctly-configured setup.
_MAX_POLL_INTERVAL = 150
AUTO_SYNC_TIME = os.getenv("AUTO_SYNC_TIME", "false").lower() == "true"
# "high" adds a per-cell HA entity for every cell in the stack (9 entities
# each) on top of "medium" — on a large stack (e.g. 16 modules x 15 cells)
# that's thousands of extra entities and a proportional jump in poll-loop
# round trips, so it must be opt-in rather than the default.
MONITORING_LEVEL = os.getenv("MONITORING_LEVEL", "medium").lower()
MAX_BATTERIES = _int_env("MAX_BATTERIES", 16)

# Path where the energy counters are persisted across container restarts.
# Override with the ENERGY_STATE_FILE env var (set to "" to disable persistence).
ENERGY_STATE_FILE = os.getenv("ENERGY_STATE_FILE", "/data/energy_state.json")

STATE_TOPIC = f"{MQTT_TOPIC_PREFIX}/state"
AVAIL_TOPIC = f"{MQTT_TOPIC_PREFIX}/availability"

# Bumped whenever the state payload's shape changes in a way the HA
# integration needs to know about. The integration (coordinator.py's
# _SCHEMA_VERSION) rejects any payload whose schema_version doesn't match,
# rather than guessing at partial compatibility between mismatched
# sidecar/integration releases installed independently via Docker vs HACS.
SCHEMA_VERSION = 1
# Informational only (not compatibility-checked) — surfaced in HA diagnostics
# so a bug report shows which sidecar build produced the data. Baked in by
# docker/Dockerfile's GIT_SHA/SIDECAR_VERSION build args (see the release
# workflow that sets them from the tag/commit being built); falls back to
# these placeholders for a manual `python main.py` run outside Docker.
SIDECAR_VERSION = os.getenv("SIDECAR_VERSION", "0.0.0-dev")
GIT_SHA = os.getenv("GIT_SHA", "unknown")

# BMS connection


# Once CONSOLE_ALT_TERMINATOR shows up without CONSOLE_PROMPT, wait this much
# longer for a trailing "pylon>" that arrived in a separate read before
# accepting the response as-is — this is just a short best-effort wait, not a
# correctness guarantee: if the prompt is slower than this,
# _stray_prompt_pending (see BmsConnection) is what actually prevents it from
# leaking into whatever gets read next, however late it eventually arrives.
_ALT_TERMINATOR_GRACE = 0.3
_READ_TIMEOUT = 5.0  # overall deadline waiting for a complete response
_POLL_INTERVAL = 0.1  # per-read timeout while polling for more data
_COMMAND_RETRIES = 2  # extra attempts after a bare read timeout, before giving up


class BmsConnection:
    """Manages a serial or TCP connection to the Pylontech BMS.

    Every command's response ends with the console's "pylon>" prompt (or, on
    some firmware, a "Command completed" line with no prompt at all), so
    reads poll in short bursts until one of those terminators appears (or a
    deadline elapses) instead of sleeping a fixed duration and grabbing
    whatever happens to be available — a fast response returns immediately, a
    slow one gets the full deadline, and a response is never returned
    truncated mid-stream.
    """

    def __init__(self) -> None:
        self._tcp: socket.socket | None = None
        self._serial: serial.Serial | None = None
        # Set when a response was accepted via CONSOLE_ALT_TERMINATOR without ever
        # seeing "pylon>" — the BMS may still emit that trailing prompt after
        # we've already moved on. It can arrive interleaved with the *next*
        # command's response instead of before it (a fixed flush-before-write
        # can't reliably beat this, since the byte may simply not exist yet
        # on the wire at flush time). Tracking it explicitly lets the next
        # read strip it wherever it actually shows up, rather than gambling
        # on timing.
        self._stray_prompt_pending: bool = False

    def _open(self) -> None:
        if CONNECTION_TYPE == "tcp":
            _LOGGER.info("Opening TCP connection to %s:%d", TCP_HOST, TCP_PORT)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((TCP_HOST, TCP_PORT))
            self._tcp = s
            # A real console sends an unsolicited banner/prompt as soon as a
            # client connects, but a transparent serial-over-TCP bridge only
            # forwards bytes — it won't print anything until prompted. Prime
            # it with a blank line like the serial path below; any leftover
            # bytes from a console's own unsolicited banner are harmless
            # since _flush_stale_input() drains them before the first real
            # command is sent.
            s.sendall(b"\n")
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
        """Read chunks until the "pylon>" prompt (or "Command completed") appears.

        Raises ConnectionError if the remote/serial side goes away, or
        TimeoutError if _READ_TIMEOUT elapses, before either terminator is
        seen. A response is either complete or an exception — it must never
        come back as a partial fragment silently mistaken for a full one
        further up the call chain (see send_command's callers, which check
        for expected substrings rather than a definitive terminator).
        """
        deadline = time.monotonic() + _READ_TIMEOUT
        grace_deadline: float | None = None
        data = b""
        while time.monotonic() < deadline:
            if CONNECTION_TYPE == "tcp":
                if self._tcp is None:
                    raise ConnectionError("TCP socket is not open")
                self._tcp.settimeout(_POLL_INTERVAL)
                try:
                    chunk = self._tcp.recv(4096)
                except TimeoutError:
                    chunk = None  # nothing arrived this tick — not EOF
                else:
                    if not chunk:
                        # A real recv() of b"" (no exception) means the peer
                        # closed the connection — always fatal, even mid-grace.
                        raise ConnectionError("BMS closed the TCP connection")
            else:
                if self._serial is None:
                    raise ConnectionError("Serial port is not open")
                chunk = self._serial.read(4096)  # b"" on timeout; serial has no EOF
            if chunk:
                data += chunk
            if self._stray_prompt_pending and data.startswith(CONSOLE_PROMPT):
                # A real response never begins with the prompt itself (the
                # console only ever prints it after content) — a leading
                # occurrence here can only be the previous exchange's
                # straggler. Strip it once and keep reading for *this*
                # command's own terminator.
                data = data[len(CONSOLE_PROMPT) :]
                self._stray_prompt_pending = False
            if CONSOLE_PROMPT in data:
                return data
            if CONSOLE_ALT_TERMINATOR in data:
                if grace_deadline is None:
                    grace_deadline = time.monotonic() + _ALT_TERMINATOR_GRACE
                elif time.monotonic() >= grace_deadline:
                    self._stray_prompt_pending = True
                    return data
        raise TimeoutError(
            f"Timed out after {_READ_TIMEOUT}s waiting for the "
            f"{CONSOLE_PROMPT.decode()!r} prompt or "
            f"{CONSOLE_ALT_TERMINATOR.decode()!r} ({len(data)} bytes received)"
        )

    def _flush_stale_input(self) -> None:
        """Discard bytes left over from an abandoned previous response.

        Without this, a retried or unrelated next command could have stale
        bytes (e.g. a late "pylon>") prepended to its response.
        """
        if CONNECTION_TYPE == "tcp":
            if self._tcp is None:
                return
            self._tcp.settimeout(0.05)
            try:
                while self._tcp.recv(4096):
                    pass
            except (TimeoutError, OSError):
                pass
        elif self._serial is not None:
            self._serial.reset_input_buffer()

    def send_command(self, cmd: str) -> str:
        """Send a command and return the ASCII response.

        A bare read timeout (nothing corrupted, just no response arrived in
        time) is retried in place up to _COMMAND_RETRIES times before giving
        up — the same transient-stall case pytes_serial's write-retry
        mechanism targets, without resending raw bytes based on serial
        buffer-fill heuristics. A ConnectionError (remote/serial side gone)
        is never retried here; that needs a full reconnect, handled by the
        poll loop's outer exception handling.
        """
        self._ensure_open()
        attempt = 0
        while True:
            self._flush_stale_input()
            if CONNECTION_TYPE == "tcp":
                if self._tcp is None:
                    raise RuntimeError("TCP socket is not open")
                self._tcp.sendall((cmd + "\n").encode("ascii"))
            else:
                if self._serial is None:
                    raise RuntimeError("Serial port is not open")
                self._serial.write((cmd + "\n").encode("ascii"))
            try:
                return self._read_until_prompt().decode("ascii", errors="ignore")
            except TimeoutError:
                attempt += 1
                if attempt > _COMMAND_RETRIES:
                    raise
                _LOGGER.warning(
                    "No response to %r within %.0fs — retrying (%d/%d)",
                    cmd,
                    _READ_TIMEOUT,
                    attempt,
                    _COMMAND_RETRIES,
                )

    def close(self) -> None:
        for conn in (self._tcp, self._serial):
            if conn is not None:
                try:
                    conn.close()
                except Exception as err:
                    _LOGGER.debug("Error closing BMS connection: %s", err)
        self._tcp = None
        self._serial = None
        self._stray_prompt_pending = False


# Energy tracker


class EnergyTracker:
    # A gap longer than this is treated as a comms outage / stall rather than
    # a real interval — its energy is dropped instead of integrated, the same
    # way invalidate_last_time() already handles an explicit reconnect gap.
    # Guards against system clock jumps and long stalls that weren't
    # explicitly invalidated inflating the counters via a huge bogus delta.
    _MAX_INTERVAL_SECONDS = 3600.0

    # Minimum spacing between disk checkpoints. At the default 15s poll
    # interval, saving on every update() would mean ~5,760 forced
    # fsync+rename cycles a day — hard on SD-card storage (the common case
    # for a Raspberry Pi sidecar) for no real benefit, since a few minutes of
    # energy counter drift on an unclean crash is immaterial. flush() bypasses
    # this for a guaranteed save on clean shutdown.
    _SAVE_INTERVAL_SECONDS = 300.0

    def __init__(self, state_file: str = "") -> None:
        self.energy_in: float = 0.0
        self.energy_out: float = 0.0
        self._last_time: float | None = None  # time.monotonic() timestamp
        self._last_power: float | None = None
        self._state_file = state_file
        self._last_save_time: float | None = None
        self._dirty = False
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
            # float() accepts "nan"/"inf" and negative numbers without
            # raising, but the HA integration's coordinator rejects
            # non-finite or negative energy_in/energy_out forever — so a
            # corrupt value here would otherwise silently brick publishing
            # until the file is manually fixed. Treat it the same as a
            # missing/corrupt file instead: reset to 0.
            if (
                not math.isfinite(energy_in)
                or not math.isfinite(energy_out)
                or energy_in < 0
                or energy_out < 0
            ):
                raise ValueError(
                    f"energy_in={energy_in!r}, energy_out={energy_out!r} "
                    "must be finite and non-negative"
                )
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
            self._last_save_time = time.monotonic()
            self._dirty = False
        except OSError as err:
            _LOGGER.warning(
                "Could not save energy state to %s: %s", self._state_file, err
            )

    def update(self, power_w: float) -> None:
        now = time.monotonic()
        if self._last_time is not None and self._last_power is not None:
            elapsed_seconds = now - self._last_time
            if 0 < elapsed_seconds <= self._MAX_INTERVAL_SECONDS:
                self._integrate(self._last_power, power_w, elapsed_seconds)
                self._dirty = True
                if self._state_file and (
                    self._last_save_time is None
                    or now - self._last_save_time >= self._SAVE_INTERVAL_SECONDS
                ):
                    self._save()
            elif elapsed_seconds > self._MAX_INTERVAL_SECONDS:
                _LOGGER.warning(
                    "Skipping energy integration over an unexpectedly long "
                    "%.0f s gap (system clock jump or stall?)",
                    elapsed_seconds,
                )
        self._last_time = now
        self._last_power = power_w

    def flush(self) -> None:
        """Force a checkpoint now, bypassing _SAVE_INTERVAL_SECONDS.

        Called on clean shutdown so whatever accumulated since the last
        periodic checkpoint isn't lost to a container restart.
        """
        if self._state_file and self._dirty:
            self._save()

    def _integrate(
        self, start_power: float, end_power: float, elapsed_seconds: float
    ) -> None:
        """Trapezoidal-integrate one interval into energy_in/energy_out.

        Assumes power varies linearly between the two endpoint samples,
        which is the best estimate available from sparse periodic polling.
        When the samples straddle zero (e.g. +500 W -> -500 W), that
        assumption implies a charge/discharge reversal partway through the
        interval — splitting at the interpolated zero-crossing captures the
        energy on both sides of it, instead of averaging the endpoints into
        a single net figure that would otherwise attribute zero energy to
        either direction despite real throughput on each side of the flip.
        """
        if start_power * end_power < 0:
            crossing_seconds = elapsed_seconds * start_power / (start_power - end_power)
            self._integrate_segment(start_power, 0.0, crossing_seconds)
            self._integrate_segment(0.0, end_power, elapsed_seconds - crossing_seconds)
        else:
            self._integrate_segment(start_power, end_power, elapsed_seconds)

    def _integrate_segment(
        self, start_power: float, end_power: float, seconds: float
    ) -> None:
        # Trapezoidal integration: average the two endpoints of this segment
        # rather than assuming it was all at one reading (rectangular/step
        # integration mis-counts whenever power changes materially between
        # polls).
        avg_power = (start_power + end_power) / 2.0
        hours = seconds / 3600.0
        kwh = abs(avg_power * hours) / 1000.0
        if avg_power >= 0:
            self.energy_in += kwh
        else:
            self.energy_out += kwh

    def invalidate_last_time(self) -> None:
        """Clear the last-poll timestamp and power sample.

        Must be called after any communication gap (BMS error, reconnect) so
        that the next update() call does not compute a kWh delta spanning the
        outage period and falsely inflate the energy counters.
        """
        self._last_time = None
        self._last_power = None


# Main


class _CommandSender(Protocol):
    """The one BmsConnection method these helpers actually need.

    Structural rather than a concrete BmsConnection type so tests can pass a
    plain send_command(cmd) -> str fake without opening a real serial/TCP
    connection.
    """

    def send_command(self, cmd: str) -> str: ...


def _fetch_batteries_indexed(bms: _CommandSender, system: PylontechSystem) -> None:
    """Populate *system* by probing 'pwr N' for each slot, up to MAX_BATTERIES.

    Fallback battery-discovery path for firmware whose aggregate 'pwr'
    response doesn't match the expected tabular format. Probing stops at the
    first "not found" (the slot index exceeds the BMS's own slot count) or
    at MAX_BATTERIES, whichever comes first; absent slots in between are
    skipped rather than treated as the end of the stack.
    """
    batteries: list[PylontechBattery] = []
    for bat_id in range(1, MAX_BATTERIES + 1):
        raw = bms.send_command(cmd_pwr_indexed(bat_id))
        if PWR_INDEXED_SCHEMA.not_found_marker in raw:
            break
        bat = _pwr_indexed_parser.parse(raw, extra={"sys_id": bat_id})
        if bat is not None:
            batteries.append(bat)

    system.batteries = batteries
    if batteries:
        system.voltage = round(sum(b.voltage for b in batteries) / len(batteries), 2)
        system.current = round(sum(b.current for b in batteries), 2)
        system.soc = round(sum(b.soc for b in batteries) / len(batteries), 1)
        system.power = round(sum(b.power for b in batteries), 1)
    else:
        system.voltage = 0.0
        system.current = 0.0
        system.soc = 0.0
        system.power = 0.0


def _enrich_batteries_indexed(bms: _CommandSender, system: PylontechSystem) -> None:
    """Add per-battery detail only the vertical 'pwr N' block exposes
    (coul_status, bat_events, power_events, sys_fault) on top of batteries
    already populated from the aggregate 'pwr' table.

    Used at MONITORING_LEVEL medium/high: the aggregate table stays the
    cheap, single-round-trip source of truth for voltage/current/soc/status,
    so this is additive detail, not a replacement — one extra round trip per
    battery, walked only when the extra detail was actually asked for.
    """
    for bat in system.batteries:
        try:
            raw = bms.send_command(cmd_pwr_indexed(bat.sys_id))
        except Exception as err:
            _LOGGER.warning(
                "Could not fetch indexed detail for battery %d: %s", bat.sys_id, err
            )
            continue
        indexed = _pwr_indexed_parser.parse(raw, extra={"sys_id": bat.sys_id})
        if indexed is None:
            continue
        bat.coul_status = indexed.coul_status
        bat.bat_events = indexed.bat_events
        bat.power_events = indexed.power_events
        bat.sys_fault = indexed.sys_fault


def _build_mqtt_client() -> mqtt.Client:
    """Construct the sidecar's paho client with auth, TLS, and LWT applied."""
    client = mqtt.Client(CallbackAPIVersion.VERSION2)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    if MQTT_TLS:
        # paho-mqtt imports `ssl` only under `if TYPE_CHECKING:`, and pyright
        # can't resolve ssl.VerifyMode from there in this version, making
        # tls_set's own inferred type partially unknown regardless of the
        # (argument-less) call here.
        client.tls_set()  # pyright: ignore[reportUnknownMemberType]
    client.will_set(AVAIL_TOPIC, "offline", retain=True)
    return client


class _PublishResult(Protocol):
    """The one MQTTMessageInfo attribute _publish_succeeded needs.

    Structural rather than the concrete paho type so tests can pass a plain
    object exposing just .rc instead of a real MQTTMessageInfo.
    """

    rc: MQTTErrorCode


def _publish_succeeded(*infos: _PublishResult) -> bool:
    """Return whether every publish() call in *infos* was actually sent.

    publish() returns MQTT_ERR_NO_CONN (it does not raise) when the client
    isn't currently connected — the message is dropped, not queued — so
    callers must check this before logging a payload as delivered.
    """
    return all(info.rc == MQTTErrorCode.MQTT_ERR_SUCCESS for info in infos)


def _warn_if_cycle_too_slow(cycle_elapsed: float) -> None:
    """Log a warning if this poll cycle risks tripping the HA staleness watchdog.

    POLL_INTERVAL is validated at startup to leave a safety margin below the
    HA integration's 300s staleness watchdog, but that only bounds the
    configured sleep — not how long fetching and publishing this cycle's
    data actually took. A slow round trip (e.g. MONITORING_LEVEL=high walking
    many batteries/cells, or a BMS responding sluggishly) can erode or blow
    through that margin even with a compliant POLL_INTERVAL, so measure it
    and warn rather than let availability flap with no explanation.
    """
    if cycle_elapsed + POLL_INTERVAL > _MAX_POLL_INTERVAL:
        _LOGGER.warning(
            "This poll cycle took %.1fs to fetch and publish — combined "
            "with POLL_INTERVAL=%ds that exceeds the %ds safety margin "
            "below the HA integration's 300s staleness watchdog. Consider a "
            "lower MONITORING_LEVEL, fewer MAX_BATTERIES, or a shorter "
            "POLL_INTERVAL",
            cycle_elapsed,
            POLL_INTERVAL,
            _MAX_POLL_INTERVAL,
        )


def _clean_shutdown(
    client: mqtt.Client, bms: "BmsConnection", energy: "EnergyTracker"
) -> None:
    """Publish offline, disconnect MQTT, and close the BMS link, then exit(0).

    Shared by every point in the poll loop that can observe a
    KeyboardInterrupt (raised by the SIGTERM handler below), so a clean
    shutdown happens regardless of which statement the signal lands on —
    not just the ones inside the main try block.
    """
    _LOGGER.info("Shutting down...")
    try:
        msg = client.publish(AVAIL_TOPIC, "offline", retain=True)
        msg.wait_for_publish(timeout=5)
    except Exception as err:
        # wait_for_publish() raises if the broker connection is down (e.g.
        # MQTT_ERR_NO_CONN during an outage) — best-effort notification only;
        # it must never prevent the cleanup below from running.
        _LOGGER.warning("Could not confirm 'offline' publish before shutdown: %s", err)
    finally:
        client.loop_stop()
        client.disconnect()
        bms.close()
        energy.flush()
    sys.exit(0)


def main() -> None:
    if not MQTT_BROKER:
        _LOGGER.error("MQTT_BROKER environment variable is required")
        sys.exit(1)
    if CONNECTION_TYPE not in ("tcp", "serial"):
        _LOGGER.error(
            "CONNECTION_TYPE must be 'tcp' or 'serial' (got %r)", CONNECTION_TYPE
        )
        sys.exit(1)
    if CONNECTION_TYPE == "tcp" and not TCP_HOST:
        _LOGGER.error("TCP_HOST is required when CONNECTION_TYPE=tcp")
        sys.exit(1)
    if CONNECTION_TYPE == "tcp" and not (1 <= TCP_PORT <= 65535):
        _LOGGER.error("TCP_PORT must be between 1 and 65535 (got %d)", TCP_PORT)
        sys.exit(1)
    if CONNECTION_TYPE == "serial" and not SERIAL_PORT:
        _LOGGER.error("SERIAL_PORT must not be empty")
        sys.exit(1)
    if not (1 <= MQTT_PORT_ENV <= 65535):
        _LOGGER.error("MQTT_PORT must be between 1 and 65535 (got %d)", MQTT_PORT_ENV)
        sys.exit(1)
    if _invalid_topic_prefix(MQTT_TOPIC_PREFIX):
        _LOGGER.error(
            "MQTT_TOPIC_PREFIX %r is invalid — must be non-empty, contain no "
            "leading/trailing whitespace or slashes, and not contain the MQTT "
            "wildcard characters '#' or '+'",
            MQTT_TOPIC_PREFIX,
        )
        sys.exit(1)
    if BAUD_RATE <= 0:
        _LOGGER.error("BAUD_RATE must be a positive integer (got %d)", BAUD_RATE)
        sys.exit(1)
    if MONITORING_LEVEL not in ("low", "medium", "high"):
        _LOGGER.error(
            "MONITORING_LEVEL must be 'low', 'medium', or 'high' (got %r)",
            MONITORING_LEVEL,
        )
        sys.exit(1)
    if POLL_INTERVAL <= 0:
        _LOGGER.error(
            "POLL_INTERVAL must be a positive integer (got %d)", POLL_INTERVAL
        )
        sys.exit(1)
    if POLL_INTERVAL > _MAX_POLL_INTERVAL:
        _LOGGER.error(
            "POLL_INTERVAL must be at most %ds — the Home Assistant "
            "integration's coordinator marks the device unavailable after "
            "%ds without a message, so anything higher will flap "
            "availability every cycle (got %ds)",
            _MAX_POLL_INTERVAL,
            _MAX_POLL_INTERVAL * 2,
            POLL_INTERVAL,
        )
        sys.exit(1)

    # Treat SIGTERM (sent by `docker stop`) the same as KeyboardInterrupt so the
    # clean-shutdown path publishes "offline" before exiting.
    def _handle_sigterm(signum: int, frame: FrameType | None) -> None:
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
    client = _build_mqtt_client()

    def on_connect(
        c: mqtt.Client,
        userdata: Any,
        flags: ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            _LOGGER.error("MQTT connect failed: %s", reason_code)
        else:
            _LOGGER.info("MQTT connected")

    def on_disconnect(
        c: mqtt.Client,
        userdata: Any,
        disconnect_flags: DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        _LOGGER.warning("MQTT disconnected: %s", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    # A SIGTERM landing here (before bms/energy exist and before the MQTT
    # client ever connected) has nothing for _clean_shutdown() to close or
    # publish through — just log and exit(0) directly rather than letting
    # the KeyboardInterrupt raised by _handle_sigterm propagate uncaught.
    def _abort_before_connected() -> None:
        _LOGGER.info("Shutting down...")
        sys.exit(0)

    _retry_delay = 5
    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT_ENV, 60)
            break
        except KeyboardInterrupt:
            _abort_before_connected()
        except Exception as err:
            _LOGGER.error(
                "Cannot connect to MQTT broker: %s — retrying in %d s",
                err,
                _retry_delay,
            )
            try:
                time.sleep(_retry_delay)
            except KeyboardInterrupt:
                _abort_before_connected()
            _retry_delay = min(_retry_delay * 2, 120)

    client.loop_start()

    # -- BMS poll loop --
    bms = BmsConnection()
    energy = EnergyTracker(state_file=ENERGY_STATE_FILE)
    system: PylontechSystem | None = None
    info_fetched = False

    while True:
        cycle_start = time.monotonic()
        try:
            if not info_fetched:
                _LOGGER.info("Fetching device info...")
                raw_info = bms.send_command(CMD_INFO)
                if system is None:
                    system = PylontechSystem(0, 0, 0, 0, 0.0, 0.0, 0.0)
                _info_parser.parse(raw_info, target=system)
                info_fetched = True

                if AUTO_SYNC_TIME:
                    _LOGGER.info("Syncing BMS time...")
                    bms.send_command(generate_time_command(datetime.now()))

            _LOGGER.debug("Polling BMS...")
            raw_pwr = bms.send_command(CMD_PWR)
            if not has_header(raw_pwr, PWR_TABLE_SCHEMA):
                time.sleep(1.0)
                raw_pwr = bms.send_command(CMD_PWR)

            if system is None:
                system = PylontechSystem(0, 0, 0, 0, 0.0, 0.0, 0.0)

            pwr_parsed = has_header(raw_pwr, PWR_TABLE_SCHEMA)
            if pwr_parsed:
                _pwr_parser.parse(raw_pwr, target=system)

            if not system.batteries or not pwr_parsed:
                # Either the aggregate table's header is missing entirely, or
                # it's present but every data row failed to parse (wrong
                # column count, corrupt firmware output, etc.) — parse_pwr
                # resets system.batteries to [] in both cases. A response
                # still missing a 'pwr' header after the retry above means
                # parse_pwr was skipped this iteration too, so system.batteries
                # (and voltage/current/soc/power) may just be leftovers from
                # the previous poll — always refall back to indexed probing in
                # that case rather than silently republishing stale readings.
                # "pwr N" uses a completely different (vertical) response
                # format — see _pwr_indexed_parser above. This correctness
                # fallback runs regardless of MONITORING_LEVEL — without it
                # there would be zero/stale battery data on such firmware at
                # any detail level.
                _LOGGER.warning(
                    "Aggregate 'pwr' response missing or yielded no valid "
                    "batteries — falling back to per-battery 'pwr N' polling"
                )
                _fetch_batteries_indexed(bms, system)
                if not system.batteries:
                    raise OSError(
                        "Did not receive valid 'pwr' response (aggregate or indexed)"
                    )
            elif MONITORING_LEVEL in ("medium", "high"):
                _enrich_batteries_indexed(bms, system)

            raw_stat = bms.send_command(CMD_STAT)
            raw_time = bms.send_command(CMD_TIME)

            if MONITORING_LEVEL == "high":
                for bat in system.batteries:
                    try:
                        raw_bat = bms.send_command(cmd_bat(bat.sys_id))
                        _bat_parser.parse(raw_bat, target=bat)
                    except Exception as bat_err:
                        _LOGGER.warning(
                            "Could not fetch cell data for battery %d: %s",
                            bat.sys_id,
                            bat_err,
                        )

            _stat_parser.parse(raw_stat, target=system)
            _time_parser.parse(raw_time, target=system)

            energy.update(system.power)
            system.energy_in = round(energy.energy_in, 3)
            system.energy_out = round(energy.energy_out, 3)

            payload_dict = asdict(system)
            payload_dict["schema_version"] = SCHEMA_VERSION
            payload_dict["sidecar_version"] = SIDECAR_VERSION
            payload_dict["sidecar_commit"] = GIT_SHA
            payload = json.dumps(payload_dict, default=str)
            state_info = client.publish(STATE_TOPIC, payload, retain=True)
            avail_info = client.publish(AVAIL_TOPIC, "online", retain=True)
            if not _publish_succeeded(state_info, avail_info):
                _LOGGER.warning(
                    "MQTT publish failed (state rc=%s, availability rc=%s) — "
                    "client not connected; this reading was not delivered",
                    state_info.rc,
                    avail_info.rc,
                )
            else:
                _LOGGER.info(
                    "Published | V=%.2fV I=%.2fA SOC=%.1f%% P=%.1fW "
                    "batteries=%d cells=%d",
                    system.voltage,
                    system.current,
                    system.soc,
                    system.power,
                    len(system.batteries),
                    sum(len(b.cells) for b in system.batteries),
                )

            _warn_if_cycle_too_slow(time.monotonic() - cycle_start)

        except (serial.SerialException, OSError) as err:
            _LOGGER.error("BMS connection error: %s — reconnecting in 5 s", err)
            client.publish(AVAIL_TOPIC, "offline", retain=True)
            bms.close()
            energy.invalidate_last_time()
            info_fetched = False
            # A KeyboardInterrupt raised here (SIGTERM landing mid-sleep) would
            # otherwise propagate straight out of this except block — sibling
            # except clauses on the same try don't catch exceptions raised
            # while already handling one — skipping _clean_shutdown entirely.
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                _clean_shutdown(client, bms, energy)
            continue
        except KeyboardInterrupt:
            _clean_shutdown(client, bms, energy)  # never returns — exits the process
        except Exception as err:
            _LOGGER.error("Unexpected error: %s", err, exc_info=True)
            client.publish(AVAIL_TOPIC, "offline", retain=True)
            # Close the BMS connection so the next poll starts fresh; the
            # exception may have left the serial/TCP socket in a broken state.
            bms.close()
            energy.invalidate_last_time()
            info_fetched = False

        # Outside the try above so a SIGTERM/KeyboardInterrupt landing here
        # (the loop's most likely resting point, given POLL_INTERVAL is
        # normally far longer than one poll) still hits the clean-shutdown
        # path instead of exiting without publishing "offline" or closing
        # the BMS connection.
        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            _clean_shutdown(client, bms, energy)


if __name__ == "__main__":
    main()
