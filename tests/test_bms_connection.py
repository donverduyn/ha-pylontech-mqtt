"""Tests for BmsConnection (docker/main.py) against the real TCP stub server.

These exercise the terminator-based read loop end-to-end over an actual
socket, rather than mocking send/recv — the whole point of the fix is
correct behavior against real (if simulated) response timing.
"""

import time

import main
import pytest
from conftest import STUB_HOST


@pytest.fixture
def tcp_bms(monkeypatch, stub_server):
    """A BmsConnection wired to the stub server over TCP."""
    monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
    monkeypatch.setattr(main, "TCP_HOST", STUB_HOST)
    monkeypatch.setattr(main, "TCP_PORT", stub_server)
    conn = main.BmsConnection()
    yield conn
    conn.close()


class TestBmsConnectionTcp:
    def test_send_command_returns_complete_response(self, tcp_bms) -> None:
        """A full, correctly-terminated response must come back, not a
        truncated fragment cut off by a fixed read window."""
        resp = tcp_bms.send_command("info")
        assert "Command completed successfully" in resp
        assert "pylon>" in resp

    def test_initial_banner_is_not_mixed_into_first_response(self, tcp_bms) -> None:
        """The unsolicited connect-time banner must be drained separately —
        it must not appear prepended to the first real command's response."""
        resp = tcp_bms.send_command("info")
        # The banner alone (no command echo) would make "info" absent from
        # the very start of the response; the parser looks for a proper
        # "Device address" line, so a leaked banner would show up as noise
        # before it rather than replacing it.
        assert "Device address" in resp

    def test_sequential_commands_do_not_leak_bytes_between_each_other(
        self, tcp_bms
    ) -> None:
        """Each response must be fully drained up to its own prompt, so the
        next command starts clean instead of inheriting stray bytes."""
        first = tcp_bms.send_command("info")
        second = tcp_bms.send_command("pwr")
        assert "Device address" in first
        assert "Power Volt" in second
        # No cross-contamination: the "pwr" response shouldn't carry a
        # second, leftover copy of the "info" response's distinctive text.
        assert second.count("Device address") == 0

    def test_fast_response_does_not_incur_fixed_delay(self, tcp_bms) -> None:
        """The old implementation always blocked for a fixed ~1-3 s per
        command regardless of how fast the reply actually arrived. A
        buffered, terminator-aware read must return as soon as the "pylon>"
        prompt shows up."""
        start = time.monotonic()
        tcp_bms.send_command("info")
        elapsed = time.monotonic() - start
        assert elapsed < 1.0

    def test_unknown_command_still_terminates_cleanly(self, tcp_bms) -> None:
        """Even an error response ends in a prompt and must be read in full."""
        resp = tcp_bms.send_command("not_a_real_command")
        assert "pylon>" in resp
