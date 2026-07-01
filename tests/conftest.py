# tests/conftest.py
"""
Shared pytest fixtures and import bootstrap for ha-pylon-integration tests.

The custom_components package pulls in `homeassistant` at __init__.py time,
which is not available outside HA.  We bypass __init__.py by registering a
namespace package manually in sys.modules, then loading parser.py / structs.py
directly via importlib.  Both files have zero HA dependencies.
"""
import importlib.util
import socket
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import bootstrap — must run before any test imports the integration code
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
_COMP = _ROOT / "custom_components" / "pylontech_serial"

# Register a namespace package so relative imports inside the module files work
_pkg = types.ModuleType("pylontech_serial")
_pkg.__path__ = [str(_COMP)]
_pkg.__package__ = "pylontech_serial"
sys.modules.setdefault("pylontech_serial", _pkg)


def _load_module(name: str, path: Path):
    """Load a single .py file into sys.modules as part of the pylontech_serial pkg."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "pylontech_serial"
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_module("pylontech_serial.structs", _COMP / "structs.py")
_load_module("pylontech_serial.parser",  _COMP / "parser.py")


# ---------------------------------------------------------------------------
# Stub server lifecycle
# ---------------------------------------------------------------------------
STUB_HOST = "127.0.0.1"
STUB_PORT = 12399          # dedicated port, unlikely to clash
STUB_BATTERIES = 2
STUB_MODEL = "US5000"      # most capable model → most field coverage
STUB_SOC_START = 75


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
    proc = subprocess.Popen(
        [
            sys.executable, str(_ROOT / "stub" / "pylon_stub.py"),
            "--host", STUB_HOST,
            "--port", str(STUB_PORT),
            "--batteries", str(STUB_BATTERIES),
            "--model", STUB_MODEL,
            "--soc", str(STUB_SOC_START),
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
def _raw_command(sock: socket.socket, cmd: str, read_pause: float = 0.4) -> str:
    """Send *cmd* over *sock* and return the full ASCII response."""
    sock.sendall((cmd + "\n").encode("ascii"))
    time.sleep(read_pause)
    data = b""
    sock.settimeout(0.5)
    try:
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    return data.decode("ascii", errors="replace")


@pytest.fixture
def stub_conn(stub_server):
    """Open a fresh TCP connection to the stub, consume the initial prompt."""
    s = socket.create_connection((STUB_HOST, stub_server), timeout=3)
    s.settimeout(2)
    time.sleep(0.15)
    try:
        s.recv(4096)          # discard initial "pylon>" prompt
    except socket.timeout:
        pass
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Convenience: parsed objects ready for assertions
# ---------------------------------------------------------------------------
@pytest.fixture
def pwr_system(stub_conn):
    from pylontech_serial.parser import PylontechParser
    raw = _raw_command(stub_conn, "pwr")
    return PylontechParser.parse_pwr(raw)


@pytest.fixture
def info_system(stub_conn):
    from pylontech_serial.parser import PylontechParser
    from pylontech_serial.structs import PylontechSystem
    raw = _raw_command(stub_conn, "info")
    sys = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
    return PylontechParser.parse_info(raw, sys)


@pytest.fixture
def stat_system(stub_conn):
    from pylontech_serial.parser import PylontechParser
    from pylontech_serial.structs import PylontechSystem
    raw = _raw_command(stub_conn, "stat")
    sys = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
    return PylontechParser.parse_stat(raw, sys)


@pytest.fixture
def time_system(stub_conn):
    from pylontech_serial.parser import PylontechParser
    from pylontech_serial.structs import PylontechSystem
    raw = _raw_command(stub_conn, "time")
    sys = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
    return PylontechParser.parse_time(raw, sys)
