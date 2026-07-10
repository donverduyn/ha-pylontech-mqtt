"""Tests for BmsConnection's serial branch (src/main.py) against a real
PTY-backed port.

CONNECTION_TYPE=serial is the sidecar's *default* (see src/main.py's
module docstring and CONNECTION_TYPE's os.getenv fallback), yet
test_bms_connection.py only ever exercises the "tcp" branch — every real
connection test in that file wires BmsConnection to the stub server over
TCP. pyserial has no in-process loopback object, but os.openpty() gives a
real (kernel-backed) serial device pair: the master fd stands in for the
BMS console (bytes BmsConnection writes arrive there; bytes written there
arrive at the Serial object's read()), so the exact terminator-based read
loop and open/reopen/flush logic run against real serial I/O timing rather
than a mocked pyserial object.
"""

import contextlib
import os
import pty
import select
import threading

import pytest

import main


@pytest.fixture
def serial_pty():
    """Yield (master_fd, slave_path) for a fresh PTY pair.

    The kernel returns EIO from the master side the instant nobody holds
    the slave open (including the gap between opening the pair here and
    BmsConnection's own serial.Serial(SERIAL_PORT, ...) open() call below)
    — so this keeps its own fd on the slave path open for the fixture's
    whole lifetime purely to hold the line open. serial.Serial opens the
    same path as a second, independent fd, exactly as it would open a real
    /dev/ttyUSB0 path; multiple opens of one tty path are fine.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    yield master_fd, slave_path
    for fd in (master_fd, slave_fd):
        with contextlib.suppress(OSError):
            os.close(fd)


class _FakeBmsResponder:
    """Background thread answering serial writes with canned responses
    ending in the "pylon>" prompt — the master side of the PTY pair,
    standing in for a real BMS console.
    """

    _RESPONSES: dict[str, bytes] = {
        "": b"pylon>",
        "info": (
            b"Device address   : 2\r\n"
            b"Manufacturer     : Pylon\r\n"
            b"Command completed successfully\r\npylon>"
        ),
        "pwr": (
            b"Power Volt Curr Tempr Coulomb ...\r\n"
            b"1    51200 1000  25000 90    ...\r\n"
            b"Command completed successfully\r\npylon>"
        ),
    }

    def __init__(self, master_fd: int) -> None:
        self._fd = master_fd
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.05)
            except OSError:
                return
            if not ready:
                continue
            try:
                chunk = os.read(self._fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.decode("ascii", errors="ignore").strip()
                reply = self._RESPONSES.get(
                    cmd, b"Command completed successfully\r\npylon>"
                )
                try:
                    os.write(self._fd, reply)
                except OSError:
                    return


@pytest.fixture
def serial_bms(monkeypatch, serial_pty):
    """A BmsConnection wired to a fake BMS console over a real PTY."""
    master_fd, slave_path = serial_pty
    monkeypatch.setattr(main, "CONNECTION_TYPE", "serial")
    monkeypatch.setattr(main, "SERIAL_PORT", slave_path)
    monkeypatch.setattr(main, "BAUD_RATE", 115200)
    responder = _FakeBmsResponder(master_fd)
    responder.start()
    conn = main.BmsConnection()
    yield conn
    conn.close()
    responder.stop()


class TestBmsConnectionSerial:
    def test_send_command_returns_complete_response(self, serial_bms) -> None:
        """Opening the port (priming write + drain), then a real command,
        must return the complete, correctly-terminated response — the same
        contract test_bms_connection_tcp.py already proves for TCP."""
        resp = serial_bms.send_command("info")
        assert "Command completed successfully" in resp
        assert "pylon>" in resp

    def test_second_command_does_not_reopen_the_port(self, serial_bms) -> None:
        """_ensure_open() must not reopen an already-open serial handle."""
        serial_bms.send_command("info")
        opened_serial = serial_bms._serial
        resp = serial_bms.send_command("pwr")
        assert "Power Volt" in resp
        assert serial_bms._serial is opened_serial

    def test_ensure_open_reopens_a_closed_but_not_none_handle(self, serial_bms) -> None:
        """A serial handle that was closed out from under BmsConnection
        (without being reset to None) must be reopened in place, not
        replaced — covers the `elif not self._serial.is_open` branch."""
        serial_bms.send_command("info")
        serial_bms._serial.close()
        assert serial_bms._serial.is_open is False

        resp = serial_bms.send_command("pwr")

        assert "Power Volt" in resp
        assert serial_bms._serial.is_open is True

    def test_close_then_send_command_opens_a_fresh_handle(self, serial_bms) -> None:
        """After close(), the next send_command must go through _open()
        again (self._serial is None) rather than raising."""
        serial_bms.send_command("info")
        serial_bms.close()
        assert serial_bms._serial is None

        resp = serial_bms.send_command("pwr")

        assert "Power Volt" in resp


class TestReadUntilPromptSerial:
    """Low-level _read_until_prompt/_flush_stale_input coverage against a
    raw serial.Serial handle wired directly to the PTY, mirroring
    test_bms_connection.py's TestReadUntilPromptTruncation (which does the
    same thing for a raw TCP socket)."""

    def _connection(self, monkeypatch, read_timeout: float = 2.0) -> main.BmsConnection:
        monkeypatch.setattr(main, "CONNECTION_TYPE", "serial")
        monkeypatch.setattr(main, "_READ_TIMEOUT", read_timeout)
        return main.BmsConnection()

    def test_read_until_prompt_raises_when_serial_not_open(self, monkeypatch) -> None:
        """conn._serial is None (never opened, or closed) must raise
        ConnectionError rather than crash on a None attribute access."""
        conn = self._connection(monkeypatch)
        assert conn._serial is None
        with pytest.raises(ConnectionError, match="Serial port is not open"):
            conn._read_until_prompt()

    def test_response_with_prompt_returns_in_full(
        self, monkeypatch, serial_pty
    ) -> None:
        import serial as pyserial

        master_fd, slave_path = serial_pty
        conn = self._connection(monkeypatch)
        conn._serial = pyserial.Serial(slave_path, 115200, timeout=0.1)
        try:
            os.write(master_fd, b"Command completed successfully\r\npylon>")
            data = conn._read_until_prompt()
            assert b"pylon>" in data
            assert b"Command completed successfully" in data
        finally:
            conn._serial.close()

    @pytest.mark.e2e
    def test_timeout_before_prompt_raises(self, monkeypatch, serial_pty) -> None:
        import serial as pyserial

        master_fd, slave_path = serial_pty
        conn = self._connection(monkeypatch, read_timeout=0.3)
        conn._serial = pyserial.Serial(slave_path, 115200, timeout=0.05)
        try:
            # no prompt, ever
            os.write(master_fd, b"Power Volt Curr ...\r\n1 51200 100")
            with pytest.raises(TimeoutError):
                conn._read_until_prompt()
        finally:
            conn._serial.close()

    def test_flush_stale_input_drains_leftover_bytes(
        self, monkeypatch, serial_pty
    ) -> None:
        """Bytes abandoned from a previous response must not leak into the
        next _read_until_prompt() call after a flush — covers the serial
        branch of _flush_stale_input (reset_input_buffer())."""
        import time

        import serial as pyserial

        master_fd, slave_path = serial_pty
        monkeypatch.setattr(main, "CONNECTION_TYPE", "serial")
        conn = main.BmsConnection()
        conn._serial = pyserial.Serial(slave_path, 115200, timeout=0.1)
        try:
            os.write(master_fd, b"stale leftover bytes from an abandoned response")
            time.sleep(0.05)  # let the bytes actually land in the kernel tty buffer
            conn._flush_stale_input()

            os.write(master_fd, b"Command completed successfully\r\npylon>")
            data = conn._read_until_prompt()

            assert b"stale leftover" not in data
            assert b"Command completed successfully" in data
        finally:
            conn._serial.close()


class TestSendCommandRetrySerial:
    """send_command's bare-timeout retry, over the serial branch — mirrors
    test_bms_connection.py's TestSendCommandRetryAndFlush for TCP."""

    @pytest.mark.e2e
    def test_retries_after_a_stall_then_succeeds(self, monkeypatch, serial_pty) -> None:
        import time

        master_fd, slave_path = serial_pty
        monkeypatch.setattr(main, "CONNECTION_TYPE", "serial")
        monkeypatch.setattr(main, "SERIAL_PORT", slave_path)
        monkeypatch.setattr(main, "BAUD_RATE", 115200)
        monkeypatch.setattr(main, "_READ_TIMEOUT", 0.3)
        monkeypatch.setattr(main, "_COMMAND_RETRIES", 2)
        conn = main.BmsConnection()

        def _server() -> None:
            os.read(master_fd, 4096)  # the priming "\n" from _open()
            os.write(master_fd, b"pylon>")
            os.read(master_fd, 4096)  # first "pwr\n" write — deliberately ignored
            time.sleep(0.5)  # outlast the client's 0.3s read timeout
            os.read(master_fd, 4096)  # the retried write
            os.write(master_fd, b"Command completed successfully\r\npylon>")

        t = threading.Thread(target=_server)
        try:
            t.start()
            result = conn.send_command("pwr")
            t.join(timeout=2)
            assert "Command completed successfully" in result
        finally:
            conn.close()

    @pytest.mark.e2e
    def test_gives_up_after_exhausting_retries(self, monkeypatch, serial_pty) -> None:
        master_fd, slave_path = serial_pty
        monkeypatch.setattr(main, "CONNECTION_TYPE", "serial")
        monkeypatch.setattr(main, "SERIAL_PORT", slave_path)
        monkeypatch.setattr(main, "BAUD_RATE", 115200)
        monkeypatch.setattr(main, "_READ_TIMEOUT", 0.2)
        monkeypatch.setattr(main, "_COMMAND_RETRIES", 1)
        conn = main.BmsConnection()

        def _server() -> None:
            os.read(master_fd, 4096)  # priming "\n"
            os.write(master_fd, b"pylon>")
            # never answers the real command at all

        t = threading.Thread(target=_server)
        try:
            t.start()
            with pytest.raises(TimeoutError):
                conn.send_command("pwr")
            t.join(timeout=2)
        finally:
            conn.close()
