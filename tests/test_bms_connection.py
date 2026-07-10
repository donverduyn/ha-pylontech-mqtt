"""Tests for BmsConnection (src/main.py) against the real TCP stub server.

These exercise the terminator-based read loop end-to-end over an actual
socket, rather than mocking send/recv — the whole point of the fix is
correct behavior against real (if simulated) response timing.
"""

import socket
import threading
import time

import pytest
from conftest import STUB_HOST

import main


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


class TestReadUntilPromptTruncation:
    """A response is either complete (terminated by the "pylon>" prompt) or
    an exception — it must never come back as a silently-accepted partial
    fragment (see BmsConnection._read_until_prompt)."""

    def _connection(
        self, monkeypatch, read_timeout: float = 0.3
    ) -> "main.BmsConnection":
        monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
        monkeypatch.setattr(main, "_READ_TIMEOUT", read_timeout)
        return main.BmsConnection()

    @pytest.mark.e2e
    def test_timeout_before_prompt_raises(self, monkeypatch) -> None:
        conn = self._connection(monkeypatch)
        server, client = socket.socketpair()
        conn._tcp = client
        try:
            server.sendall(b"Power Volt Curr ...\n1 51200 100")  # no prompt, ever
            with pytest.raises(TimeoutError):
                conn._read_until_prompt()
        finally:
            server.close()
            client.close()

    def test_remote_close_before_prompt_raises(self, monkeypatch) -> None:
        conn = self._connection(monkeypatch, read_timeout=2.0)
        server, client = socket.socketpair()
        conn._tcp = client
        try:
            server.sendall(b"Power Volt Curr ...\n1 51200 100")
            server.close()  # hang up mid-response, before the prompt
            with pytest.raises(ConnectionError):
                conn._read_until_prompt()
        finally:
            client.close()

    def test_response_with_prompt_returns_in_full(self, monkeypatch) -> None:
        conn = self._connection(monkeypatch, read_timeout=2.0)
        server, client = socket.socketpair()
        conn._tcp = client
        try:
            server.sendall(b"Command completed successfully\r\npylon>")
            data = conn._read_until_prompt()
            assert b"pylon>" in data
            assert b"Command completed successfully" in data
        finally:
            server.close()
            client.close()

    @pytest.mark.e2e
    def test_response_with_only_alt_terminator_returns_without_prompt(
        self, monkeypatch
    ) -> None:
        """Some Pytes/Pylontech firmware completes a command without ever
        emitting the "pylon>" prompt. That must still return promptly
        instead of timing out after _READ_TIMEOUT."""
        conn = self._connection(monkeypatch, read_timeout=2.0)
        monkeypatch.setattr(main, "_ALT_TERMINATOR_GRACE", 0.05)
        server, client = socket.socketpair()
        conn._tcp = client
        try:
            server.sendall(b"Voltage : 51200\r\nCommand completed\r\n")
            data = conn._read_until_prompt()
            assert b"Command completed" in data
            assert b"pylon>" not in data
        finally:
            server.close()
            client.close()

    @pytest.mark.e2e
    def test_prompt_later_than_grace_window_does_not_leak_into_next_read(
        self, monkeypatch
    ) -> None:
        """A 'pylon>' that arrives *after_ALT_TERMINATOR_GRACE has already
        elapsed cannot be captured by the current read — it's still in
        flight, not yet on the wire, when the grace window gives up. It
        must instead be recognised and stripped from the *next* read
        instead of getting prepended to that next command's real response
        (see BmsConnection._stray_prompt_pending)."""
        conn = self._connection(monkeypatch, read_timeout=2.0)
        monkeypatch.setattr(main, "_ALT_TERMINATOR_GRACE", 0.05)
        server, client = socket.socketpair()
        conn._tcp = client
        try:
            server.sendall(b"Command completed successfully\r\n")
            first = conn._read_until_prompt()
            assert b"pylon>" not in first
            assert conn._stray_prompt_pending is True

            # The straggler prompt shows up now, glued to the front of the
            # *next* command's real response — exactly the race the fix
            # targets.
            server.sendall(b"pylon>Power Volt Curr\r\n1 51200 100\r\npylon>")
            second = conn._read_until_prompt()

            assert second == b"Power Volt Curr\r\n1 51200 100\r\npylon>"
            assert conn._stray_prompt_pending is False
        finally:
            server.close()
            client.close()

    @pytest.mark.e2e
    def test_late_prompt_within_grace_window_is_absorbed(self, monkeypatch) -> None:
        """A 'pylon>' that arrives in a separate read shortly after 'Command
        completed' must still be captured here — not left to leak into the
        next command's response (see _ALT_TERMINATOR_GRACE)."""
        conn = self._connection(monkeypatch, read_timeout=2.0)
        monkeypatch.setattr(main, "_ALT_TERMINATOR_GRACE", 0.2)
        server, client = socket.socketpair()
        conn._tcp = client
        try:
            server.sendall(b"Command completed\r\n")

            def _send_late_prompt() -> None:
                time.sleep(0.05)
                server.sendall(b"pylon>")

            t = threading.Thread(target=_send_late_prompt)
            t.start()
            data = conn._read_until_prompt()
            t.join(timeout=1)

            assert b"pylon>" in data
        finally:
            server.close()
            client.close()


class TestSendCommandRetryAndFlush:
    """send_command retries a bare read timeout in place before giving up,
    and flushes stale unread bytes before every write so a retry (or the
    next command entirely) never inherits contamination from an abandoned
    response — see BmsConnection.send_command / _flush_stale_input."""

    def _connection(
        self, monkeypatch, *, read_timeout: float, retries: int
    ) -> "main.BmsConnection":
        monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
        monkeypatch.setattr(main, "_READ_TIMEOUT", read_timeout)
        monkeypatch.setattr(main, "_COMMAND_RETRIES", retries)
        return main.BmsConnection()

    @pytest.mark.e2e
    def test_retries_after_a_stall_then_succeeds(self, monkeypatch) -> None:
        conn = self._connection(monkeypatch, read_timeout=0.3, retries=2)
        server, client = socket.socketpair()
        conn._tcp = client

        def _server() -> None:
            server.recv(4096)  # first "pwr\n" write — deliberately ignored
            time.sleep(0.5)  # outlast the client's 0.3s read timeout
            server.recv(4096)  # the retried write
            server.sendall(b"Command completed successfully\r\npylon>")

        t = threading.Thread(target=_server)
        try:
            t.start()
            result = conn.send_command("pwr")
            t.join(timeout=2)
            assert "Command completed successfully" in result
        finally:
            server.close()
            client.close()

    @pytest.mark.e2e
    def test_gives_up_after_exhausting_retries(self, monkeypatch) -> None:
        conn = self._connection(monkeypatch, read_timeout=0.2, retries=1)
        server, client = socket.socketpair()
        conn._tcp = client
        try:
            with pytest.raises(TimeoutError):
                conn.send_command("pwr")  # server never responds at all
        finally:
            server.close()
            client.close()

    def test_flush_stale_input_drains_leftover_bytes(self, monkeypatch) -> None:
        """Bytes abandoned from a previous response must not leak into the
        next _read_until_prompt() call after a flush."""
        monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
        conn = main.BmsConnection()
        server, client = socket.socketpair()
        conn._tcp = client
        try:
            server.sendall(b"stale leftover bytes from an abandoned response")
            time.sleep(0.05)  # let the bytes actually land in the socket buffer
            conn._flush_stale_input()

            server.sendall(b"Command completed successfully\r\npylon>")
            data = conn._read_until_prompt()

            assert b"stale leftover" not in data
            assert b"Command completed successfully" in data
        finally:
            server.close()
            client.close()
