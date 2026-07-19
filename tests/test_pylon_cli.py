"""Tests for scripts/pylon_cli.py — the interactive console client.

Runs the script as a real subprocess against the shared stub_server fixture
(see conftest.py), the same way tests/conftest.py's StubProcess exercises
scripts/pylon_stub.py itself — this is a CLI, not an importable library, so
its own argument parsing, env-var wiring and stdout formatting only get
covered by actually invoking it.
"""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import STUB_HOST

_ROOT = Path(__file__).parent.parent
_PYLON_CLI = _ROOT / "scripts" / "pylon_cli.py"
_BIN_PYLON_CLI = _ROOT / ".devcontainer" / "bin" / "pylon_cli"
# stub_server (conftest.py) runs STUB_MODEL="US5000" -> MODELS["US5000"]["device_name"]
# in scripts/pylon_stub.py.
_STUB_DEVICE_NAME = "US5KBPL"


def _run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_PYLON_CLI), *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_one_shot_command_prints_response_and_exits(stub_server):
    result = _run("--tcp", f"{STUB_HOST}:{stub_server}", "info")

    assert result.returncode == 0
    assert _STUB_DEVICE_NAME in result.stdout
    assert "Device address" in result.stdout
    assert "Command completed successfully" in result.stdout


def test_one_shot_multi_token_command_is_joined(stub_server):
    result = _run("--tcp", f"{STUB_HOST}:{stub_server}", "pwr", "1")

    assert result.returncode == 0
    assert "Power 1" in result.stdout
    assert "Voltage" in result.stdout


def test_unknown_command_reports_error_but_exits_zero(stub_server):
    result = _run("--tcp", f"{STUB_HOST}:{stub_server}", "bogus")

    assert result.returncode == 0
    assert "Unknown command 'bogus'" in result.stdout


def test_raw_flag_shows_unnormalized_wire_bytes(stub_server):
    result = _run("--tcp", f"{STUB_HOST}:{stub_server}", "--raw", "time")

    # Cleaned output collapses \r/\n framing; --raw must preserve it verbatim,
    # shown as a Python repr (see scripts/pylon_cli.py's _format).
    assert result.stdout.startswith("'")
    assert "\\r" in result.stdout


def test_repl_runs_multiple_commands_until_exit(stub_server):
    result = _run(
        "--tcp", f"{STUB_HOST}:{stub_server}", input_text="info\nstat\nexit\n"
    )

    assert result.returncode == 0
    assert "Device address" in result.stdout
    assert "Charge Times" in result.stdout


def test_repl_ends_on_eof_without_exit_command(stub_server):
    result = _run("--tcp", f"{STUB_HOST}:{stub_server}", input_text="info\n")

    assert result.returncode == 0
    assert "Device address" in result.stdout


def test_invalid_tcp_argument_is_a_clean_usage_error():
    result = _run("--tcp", "not-a-host-port", "pwr")

    assert result.returncode == 2
    assert "expected HOST:PORT" in result.stderr


def test_out_of_range_tcp_port_is_a_clean_usage_error():
    result = _run("--tcp", "127.0.0.1:99999", "pwr")

    assert result.returncode == 2
    assert "port must be 1-65535" in result.stderr


def test_tcp_and_serial_are_mutually_exclusive():
    result = _run("--tcp", "127.0.0.1:1234", "--serial", "/dev/ttyUSB0", "pwr")

    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr


def test_connection_refused_reports_a_clean_error_not_a_traceback():
    """A refused TCP connection (nothing listening on the target port)
    raises ConnectionRefusedError from BmsConnection._open() -- a subclass
    of OSError, not ConnectionError, so this only started passing once
    _send_or_report's except clause was broadened from
    (ConnectionError, TimeoutError) to (OSError, RuntimeError). Before that
    fix, this test would see an uncaught traceback and a nonzero exit
    unrelated to argparse.
    """
    # Bind and immediately release a port -- nothing is listening there for
    # the CLI's connection attempt, and on loopback that refusal is
    # near-instant (no timeout to wait out), unlike simulating a live
    # mid-session drop.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    result = _run("--tcp", f"127.0.0.1:{port}", "info")

    assert result.returncode == 1
    assert "[pylon_cli] error:" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(
    not _BIN_PYLON_CLI.exists(),
    reason="mutmut's isolated mutants/ workspace only copies source_paths/"
    "also_copy (see pyproject.toml's [tool.mutmut]) — .devcontainer/ isn't in "
    "either, so the symlink this test targets doesn't exist there",
)
def test_bin_symlink_resolves_to_the_same_script(stub_server):
    """.devcontainer/bin/pylon_cli (the thing PATH actually points at — see
    .devcontainer/postCreate.sh) must resolve to scripts/pylon_cli.py and run
    identically, not just the script invoked by its own path."""
    assert _BIN_PYLON_CLI.is_symlink()
    assert _BIN_PYLON_CLI.resolve() == _PYLON_CLI.resolve()

    result = subprocess.run(
        [
            sys.executable,
            str(_BIN_PYLON_CLI),
            "--tcp",
            f"{STUB_HOST}:{stub_server}",
            "info",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "Device address" in result.stdout


def test_no_connection_flags_falls_back_to_connection_type_env(stub_server):
    """With neither --tcp nor --serial given, main.py's own env vars decide
    the transport — so an existing sidecar .env works with this CLI unchanged."""
    env = {
        **os.environ,
        "CONNECTION_TYPE": "tcp",
        "TCP_HOST": STUB_HOST,
        "TCP_PORT": str(stub_server),
    }
    result = subprocess.run(
        [sys.executable, str(_PYLON_CLI), "info"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0
    assert "Device address" in result.stdout
