"""
Shared pytest fixtures and import bootstrap for ha-pylontech-mqtt tests.

`pyproject.toml` puts the repo root and `src/` on ``pythonpath``, so both
`custom_components.pylontech_mqtt.*` and the HA-independent `parser` /
`parser_schema` / `structs` modules import normally with no manual
sys.modules wiring needed.
"""

import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).parent.parent


# Socket access helpers
#
# pytest-homeassistant-custom-component blocks all TCP sockets in its
# pytest_runtest_setup() hook.  The stub-based tests need real TCP connections
# to 127.0.0.1; HA integration tests mock their connections and are unaffected.
# Keeping this global avoids repeatedly paying the HA socket-blocking setup
# cost while still limiting real traffic to loopback.
def _enable_sockets() -> None:
    """Re-enable real TCP sockets (no-op when pytest-socket is not installed)."""
    try:
        # installed by pytest-homeassistant-custom-component
        import pytest_socket as _ps

        _ps.enable_socket()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _restore_sockets_per_test() -> Generator[None]:
    """Re-enable sockets for each test after the HA plugin's per-test blocking."""
    _enable_sockets()
    yield


# HA test infrastructure: point the config dir at the project root so that
# homeassistant's integration loader finds custom_components/pylontech_mqtt.
# This fixture only takes effect for tests that use the ``hass`` fixture.
@pytest.fixture
def hass_config_dir() -> str:
    """Return the project root as HA's config directory."""
    return str(_ROOT)


# Shared config-flow patch targets
#
# Every test that drives the config/options flow without a real MQTT broker
# patches these same two boundaries. Defined once here so a future rename of
# either target can't drift out of sync across test files.
PATCH_CONN = "custom_components.pylontech_mqtt.config_flow._test_mqtt_connection"
PATCH_SETUP = "custom_components.pylontech_mqtt.coordinator.PylontechCoordinator.setup"


def make_coordinator(
    hass, *, topic_prefix: str = "pylontech/stack", mqtt_tls: bool = False
):
    """Build a bare PylontechCoordinator wired to *hass* (MQTT client not started)."""
    from custom_components.pylontech_mqtt.coordinator import PylontechCoordinator
    from custom_components.pylontech_mqtt.entity import stack_id_from_broker

    return PylontechCoordinator(
        hass=hass,
        mqtt_host="localhost",
        mqtt_port=1883,
        mqtt_user="",
        mqtt_pass="",
        topic_prefix=topic_prefix,
        stack_id=stack_id_from_broker("localhost", 1883, topic_prefix),
        mqtt_tls=mqtt_tls,
    )


async def create_config_entry(hass, entry_data: dict):
    """Drive the user config flow to create an entry; return (entry, coordinator)."""
    from homeassistant import config_entries

    from custom_components.pylontech_mqtt.const import DOMAIN

    with patch(PATCH_CONN, return_value=None), patch(PATCH_SETUP):
        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(init["flow_id"], entry_data)
        await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert entries, "Config entry was not created"
    entry = entries[0]
    return entry, hass.data[DOMAIN][entry.entry_id]


# Stub server lifecycle
STUB_HOST = "127.0.0.1"
STUB_BATTERIES = 2
STUB_MODEL = "US5000"  # most capable model → most field coverage
STUB_SOC_START = 75
STUB_CELLS = 15  # all current models (US2000/US3000/US5000) have 15 cells
# Use the old (pre-*.Id) firmware layout so the parser's fallback defaults
# (which assume the old column positions) match the data rows in tests that
# intentionally strip the header line.
STUB_FIRMWARE = "old"


# The startup handshake line pylon_stub.py prints once its listening socket
# is bound (with the OS-assigned port when spawned with --port 0). Keep in
# sync with the matching print() at the bottom of scripts/pylon_stub.py.
_STUB_READY_RE = re.compile(r"\[stub\] listening on [^:]+:(\d+)")


class StubProcess:
    """A pylon_stub.py subprocess with its stdout continuously drained.

    Wraps the raw Popen to fix three failure modes the previous
    fixed-port + connect-poll approach had:

    * Port collisions: the stub is spawned with --port 0 so the OS picks a
      free port, and .port is parsed from the stub's own "listening on"
      handshake line — two test sessions running concurrently (parallel CI
      jobs, two checkouts, an agent and a human) can no longer race each
      other for a hardcoded port, with the loser's tests failing in
      ConnectionRefusedError long after the wrong stub answered the
      port-open probe.
    * Pipe deadlock: the stub logs a line per connect/disconnect and its
      stdout was a PIPE nothing ever read, so a long enough session would
      eventually fill the 64 KiB pipe buffer and block the stub mid-print.
      A daemon reader thread drains it for the process's whole lifetime.
    * Silent death: waiting on the handshake line (instead of polling the
      port) means a stub that crashes at startup or mid-session surfaces as
      one RuntimeError carrying everything it printed, not as opaque
      per-test ConnectionRefusedErrors with the traceback discarded.
    """

    def __init__(self, proc: "subprocess.Popen[str]") -> None:
        self.proc = proc
        self.port: int | None = None
        self._lines: list[str] = []
        self._ready = threading.Event()
        self._reader = threading.Thread(target=self._drain_stdout, daemon=True)
        self._reader.start()

    def _drain_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.append(line)
            if not self._ready.is_set():
                match = _STUB_READY_RE.search(line)
                if match:
                    self.port = int(match.group(1))
                    self._ready.set()
        # EOF (process exited): unblock wait_ready() even if the handshake
        # line never came, so it can report the failure instead of hanging.
        self._ready.set()

    @property
    def output(self) -> str:
        """Everything the stub has printed so far (stdout+stderr merged)."""
        return "".join(self._lines)

    def wait_ready(self, timeout: float = 10.0) -> int:
        """Block until the stub reports its bound port; return that port."""
        if not self._ready.wait(timeout) or self.port is None:
            exit_code = self.proc.poll()
            self.stop()
            reason = (
                f"exited with code {exit_code}"
                if exit_code is not None
                else f"did not report a listening port within {timeout}s"
            )
            raise RuntimeError(
                f"pylon_stub.py {reason}. Output:\n{self.output or '<no output>'}"
            )
        return self.port

    def stop(self) -> None:
        """Terminate the stub (escalating to SIGKILL) and reap it."""
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._reader.join(timeout=5)


def start_stub(*extra_args: str, port: int = 0) -> StubProcess:
    """Spawn scripts/pylon_stub.py and wait until it is accepting connections.

    Binds an OS-assigned free port by default; pass ``port=`` only when a
    test must revive a stub on the exact port a previous one used (e.g. the
    sidecar-reconnect e2e test). ``-u`` keeps the stub's later per-connection
    log lines unbuffered so the drain thread (and a post-mortem .output read)
    sees them promptly, not only at exit.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(_ROOT / "scripts" / "pylon_stub.py"),
            "--host",
            STUB_HOST,
            "--port",
            str(port),
            *extra_args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    stub = StubProcess(proc)
    stub.wait_ready()
    return stub


def _start_variant_stub(*extra_args: str) -> StubProcess:
    """Start pylon_stub.py (on an OS-assigned port) with fixed base config
    + *extra_args*. Shared by every test module that needs a stub instance
    configured differently from the session-scoped ``stub_server`` (new
    firmware, multi-group topology, etc.) — each gets its own process on its
    own port, fully independent of the session stub and of each other."""
    return start_stub(
        "--batteries",
        "2",
        "--model",
        "US5000",
        "--soc",
        "75",
        *extra_args,
    )


@pytest.fixture(scope="module")
def new_fw_stub():
    """Stub with --firmware new (23-column layout, *.Id columns present)."""
    _enable_sockets()
    stub = _start_variant_stub("--firmware", "new")
    try:
        yield stub.port
    finally:
        stub.stop()


@pytest.fixture
def new_fw_conn(new_fw_stub):
    s = socket.create_connection((STUB_HOST, new_fw_stub), timeout=3)
    _drain_prompt(s)
    yield s
    s.close()


@pytest.fixture(scope="session")
def stub_server():
    """Start pylon_stub.py once for the whole test session; yield the port."""
    # session fixture runs after pytest_runtest_setup() blocks sockets
    _enable_sockets()
    stub = start_stub(
        "--batteries",
        str(STUB_BATTERIES),
        "--model",
        STUB_MODEL,
        "--firmware",
        STUB_FIRMWARE,
        "--soc",
        str(STUB_SOC_START),
        # Well past any realistic suite runtime: session-scoped fixtures
        # like pwr_system capture their values once, lazily, whenever a
        # test first needs them — with the stub's real default 30s tick,
        # a slow run (or one where many prior tests delay that first
        # capture) could have the stub's background updater fire before
        # the capture happens, flipping soc/current away from the exact
        # values tests assert. See scripts/pylon_stub.py's _state_updater.
        "--tick-interval",
        "3600",
    )
    try:
        yield stub.port
    finally:
        stub.stop()


# Per-test TCP connection helpers
def _drain_prompt(sock: socket.socket) -> None:
    """Read and discard the initial 'pylon>' banner sent on new connections."""
    data = b""
    sock.settimeout(0.1)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"pylon>" in data:
                break
        except TimeoutError:
            break


def _raw_command(sock: socket.socket, cmd: str, timeout: float = 3.0) -> str:
    """Send *cmd* over *sock* and return the full ASCII response.

    Returns as soon as the 'pylon>' prompt appears or *timeout* seconds elapse.
    """
    sock.sendall((cmd + "\n").encode("ascii"))
    data = b""
    deadline = time.monotonic() + timeout
    sock.settimeout(0.05)
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
            if b"pylon>" in data:
                break
        except TimeoutError:
            if b"pylon>" in data:
                break
    return data.decode("ascii", errors="replace")


@pytest.fixture(scope="session")
def _session_conn(stub_server):
    """Single persistent connection used by session-scoped parsed fixtures."""
    _enable_sockets()
    s = socket.create_connection((STUB_HOST, stub_server), timeout=3)
    _drain_prompt(s)
    yield s
    s.close()


@pytest.fixture(scope="class")
def stub_conn_class(stub_server):
    """Class-scoped TCP connection shared across all tests in one class."""
    _enable_sockets()
    s = socket.create_connection((STUB_HOST, stub_server), timeout=3)
    _drain_prompt(s)
    yield s
    s.close()


@pytest.fixture
def stub_conn(stub_server):
    """Open a fresh TCP connection to the stub, consume the initial prompt."""
    s = socket.create_connection((STUB_HOST, stub_server), timeout=3)
    _drain_prompt(s)
    yield s
    s.close()


class _SchemaParserAdapter:
    """Test-only adapter exposing per-command method names (parse_pwr,
    parse_bat, ...) on top of src/parser.py's schema-driven Parser +
    src/parser_schema.py's schemas — the exact parser src/main.py runs in
    production. Test bodies across this file call e.g. ``PylontechParser.
    parse_pwr(raw)`` (see test_parser.py's top-of-file import alias) purely
    because that's a convenient, familiar shape for a test to call; src/
    main.py itself never shapes its calls this way — it instantiates
    ``Parser(SOME_SCHEMA)`` directly, once per BMS command, at import time.
    """

    @staticmethod
    def has_pwr_header(raw_text):
        from parser import has_header
        from parser_schema import PWR_TABLE_SCHEMA

        return has_header(raw_text, PWR_TABLE_SCHEMA)

    @staticmethod
    def parse_pwr(raw_text, current_system=None):
        from parser import Parser
        from parser_schema import PWR_TABLE_SCHEMA
        from structs import PylontechSystem

        if current_system is None:
            current_system = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
        Parser(PWR_TABLE_SCHEMA).parse(raw_text, target=current_system)
        return current_system

    @staticmethod
    def parse_number(value):
        from parser import parse_number

        return parse_number(value)

    @staticmethod
    def parse_pwr_indexed(raw_text, bat_id):
        from parser import Parser
        from parser_schema import PWR_INDEXED_SCHEMA

        return Parser(PWR_INDEXED_SCHEMA).parse(raw_text, extra={"sys_id": bat_id})

    @staticmethod
    def parse_info(raw_text, system):
        from parser import Parser
        from parser_schema import INFO_SCHEMA

        Parser(INFO_SCHEMA).parse(raw_text, target=system)
        return system

    @staticmethod
    def parse_stat(raw_text, system):
        from parser import Parser
        from parser_schema import STAT_FIELDS

        Parser(STAT_FIELDS).parse(raw_text, target=system)
        return system

    @staticmethod
    def parse_time(raw_text, system):
        from parser import Parser
        from parser_schema import TIME_FIELDS

        Parser(TIME_FIELDS).parse(raw_text, target=system)
        return system

    @staticmethod
    def generate_time_command(timestamp):
        from parser_schema import generate_time_command

        return generate_time_command(timestamp)

    @staticmethod
    def parse_bat(raw_text, battery):
        from parser import Parser
        from parser_schema import BAT_TABLE_SCHEMA

        Parser(BAT_TABLE_SCHEMA).parse(raw_text, target=battery)
        return battery


# Convenience: parsed objects ready for assertions, via _SchemaParserAdapter
# (the exact parser src/main.py runs in production, see above).
@pytest.fixture(scope="session")
def parser_impl():
    return _SchemaParserAdapter


@pytest.fixture(scope="session")
def pwr_system(parser_impl, _session_conn):
    raw = _raw_command(_session_conn, "pwr")
    return parser_impl.parse_pwr(raw)


@pytest.fixture(scope="session")
def info_system(parser_impl, _session_conn):
    from structs import PylontechSystem

    raw = _raw_command(_session_conn, "info")
    sys = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
    return parser_impl.parse_info(raw, sys)


@pytest.fixture(scope="session")
def stat_system(parser_impl, _session_conn):
    from structs import PylontechSystem

    raw = _raw_command(_session_conn, "stat")
    sys = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
    return parser_impl.parse_stat(raw, sys)


@pytest.fixture(scope="session")
def time_system(parser_impl, _session_conn):
    from structs import PylontechSystem

    raw = _raw_command(_session_conn, "time")
    sys = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
    return parser_impl.parse_time(raw, sys)


@pytest.fixture(scope="session")
def bat_battery(parser_impl, _session_conn, pwr_system):
    """Return the first battery from pwr_system with its cells populated
    by parsing the 'bat 1' response from the stub."""
    bat = pwr_system.batteries[0]
    raw = _raw_command(_session_conn, f"bat {bat.sys_id}")
    parser_impl.parse_bat(raw, bat)
    return bat
