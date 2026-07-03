# tests/conftest.py
"""
Shared pytest fixtures and import bootstrap for ha-pylontech-mqtt tests.

The custom_components package pulls in `homeassistant` at __init__.py time,
which is not available outside HA.  We bypass __init__.py by registering a
namespace package manually in sys.modules, then loading pylontech_parser.py / structs.py
directly via importlib.  Both files have zero HA dependencies.
"""

import importlib.util
import socket
import subprocess
import sys
import time
import types
from collections.abc import Generator
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_COMP = _ROOT / "custom_components" / "pylontech_mqtt"


# ---------------------------------------------------------------------------
# Socket access helpers
#
# pytest-homeassistant-custom-component blocks all TCP sockets in its
# pytest_runtest_setup() hook.  The stub-based tests need real TCP connections
# to 127.0.0.1; HA integration tests mock their connections and are unaffected.
# ---------------------------------------------------------------------------
def _enable_sockets() -> None:
    """Re-enable real TCP sockets (no-op when pytest-socket is not installed)."""
    try:
        import pytest_socket as _ps  # installed by pytest-homeassistant-custom-component

        _ps.enable_socket()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _restore_sockets_per_test() -> Generator[None, None, None]:
    """Re-enable sockets for each test after the HA plugin's per-test blocking."""
    _enable_sockets()
    yield


# ---------------------------------------------------------------------------
# HA test infrastructure: point the config dir at the project root so that
# homeassistant's integration loader finds custom_components/pylontech_mqtt.
# This fixture only takes effect for tests that use the ``hass`` fixture.
# ---------------------------------------------------------------------------
@pytest.fixture
def hass_config_dir() -> str:
    """Return the project root as HA's config directory."""
    return str(_ROOT)


# Register a namespace package so relative imports inside the module files work
_pkg = types.ModuleType("pylontech_mqtt")
_pkg.__path__ = [str(_COMP)]
_pkg.__package__ = "pylontech_mqtt"
sys.modules.setdefault("pylontech_mqtt", _pkg)


def _load_module(name: str, path: Path):
    """Load a single .py file into sys.modules as part of the pylontech_mqtt pkg."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None:
        raise ImportError(f"Cannot find module spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "pylontech_mqtt"
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


_load_module("pylontech_mqtt.capacity", _COMP / "capacity.py")


# ---------------------------------------------------------------------------
# Stub server lifecycle
# ---------------------------------------------------------------------------
STUB_HOST = "127.0.0.1"
STUB_PORT = 12399  # dedicated port, unlikely to clash
STUB_BATTERIES = 2
STUB_MODEL = "US5000"  # most capable model → most field coverage
STUB_SOC_START = 75
STUB_CELLS = 15  # all current models (US2000/US3000/US5000) have 15 cells
# Use the old (pre-*.Id) firmware layout so the parser's fallback defaults
# (which assume the old column positions) match the data rows in tests that
# intentionally strip the header line.
STUB_FIRMWARE = "old"


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"Stub server did not start on {host}:{port} within {timeout}s")


@pytest.fixture(scope="session")
def stub_server():
    """Start pylon_stub.py once for the whole test session; yield the port."""
    _enable_sockets()  # session fixture runs after pytest_runtest_setup() blocks sockets
    proc = subprocess.Popen(
        [
            sys.executable,
            str(_ROOT / "scripts" / "pylon_stub.py"),
            "--host",
            STUB_HOST,
            "--port",
            str(STUB_PORT),
            "--batteries",
            str(STUB_BATTERIES),
            "--model",
            STUB_MODEL,
            "--firmware",
            STUB_FIRMWARE,
            "--soc",
            str(STUB_SOC_START),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_port(STUB_HOST, STUB_PORT)
        yield STUB_PORT
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Per-test TCP connection helpers
# ---------------------------------------------------------------------------
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
        except socket.timeout:
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
        except socket.timeout:
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


# ---------------------------------------------------------------------------
# Convenience: parsed objects ready for assertions
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def pwr_system(_session_conn):
    from pylontech_parser import PylontechParser

    raw = _raw_command(_session_conn, "pwr")
    return PylontechParser.parse_pwr(raw)


@pytest.fixture(scope="session")
def info_system(_session_conn):
    from pylontech_parser import PylontechParser
    from structs import PylontechSystem

    raw = _raw_command(_session_conn, "info")
    sys = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
    return PylontechParser.parse_info(raw, sys)


@pytest.fixture(scope="session")
def stat_system(_session_conn):
    from pylontech_parser import PylontechParser
    from structs import PylontechSystem

    raw = _raw_command(_session_conn, "stat")
    sys = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
    return PylontechParser.parse_stat(raw, sys)


@pytest.fixture(scope="session")
def time_system(_session_conn):
    from pylontech_parser import PylontechParser
    from structs import PylontechSystem

    raw = _raw_command(_session_conn, "time")
    sys = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
    return PylontechParser.parse_time(raw, sys)


@pytest.fixture(scope="session")
def bat_battery(_session_conn, pwr_system):
    """Return the first battery from pwr_system with its cells populated
    by parsing the 'bat 1' response from the stub."""
    from pylontech_parser import PylontechParser

    bat = pwr_system.batteries[0]
    raw = _raw_command(_session_conn, f"bat {bat.sys_id}")
    PylontechParser.parse_bat(raw, bat)
    return bat
