#!/usr/bin/env python3
"""
PTY <-> TCP bridge for scripts/pylon_stub.py
=============================================
Exposes the stub's TCP console through a real pseudo-terminal, so
serial-only tooling — src/main.py's CONNECTION_TYPE=serial branch,
minicom, screen, picocom — can connect to it exactly as it would to a
real BMS on /dev/ttyUSB0, without real hardware or a USB-serial adapter.

Usage
-----
  # Bridge to a stub already running elsewhere:
  python scripts/pylon_stub.py --port 12300 &
  python scripts/pty_bridge.py --tcp-port 12300

  # Or let this script spawn its own stub (any pylon_stub.py flag works
  # after --):
  python scripts/pty_bridge.py --spawn-stub -- --batteries 4 --model US5000

Once running it prints the slave device path, e.g.:

  [bridge] PTY ready at /dev/pts/7
  [bridge]   point SERIAL_PORT there, or try: screen /dev/pts/7 115200

Pass --link to also symlink that path to a fixed, memorable name (the
actual /dev/pts/N number is reassigned by the kernel every run).

Ctrl-C stops the bridge (and the spawned stub, if any).
"""

import argparse
import contextlib
import os
import re
import select
import socket
import subprocess
import sys
import threading
import tty
from pathlib import Path

_STUB_READY_RE = re.compile(r"\[stub\] listening on [^:]+:(\d+)")


def _spawn_stub(
    host: str, port: int, extra_args: list[str]
) -> tuple[subprocess.Popen[str], int]:
    """Start scripts/pylon_stub.py, blocking until its "listening on" line
    appears, and keep draining its stdout afterwards so the pipe never
    fills and the stub's own [stub] log lines still show up here."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(Path(__file__).parent / "pylon_stub.py"),
            "--host",
            host,
            "--port",
            str(port),
            *extra_args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    ready = threading.Event()
    bound_port: list[int] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            if not ready.is_set():
                match = _STUB_READY_RE.search(line)
                if match:
                    bound_port.append(int(match.group(1)))
                    ready.set()
        ready.set()  # EOF: unblock even if the stub died before printing

    threading.Thread(target=_drain, daemon=True).start()
    if not ready.wait(timeout=10) or not bound_port:
        proc.terminate()
        raise RuntimeError("stub did not report a bound port within 10s")
    return proc, bound_port[0]


def _bridge(master_fd: int, sock: socket.socket) -> None:
    """Shuttle bytes between the PTY master fd and the TCP socket until
    either side closes."""
    sock_fd = sock.fileno()
    while True:
        try:
            ready, _, _ = select.select([master_fd, sock_fd], [], [])
        except OSError:
            return
        if master_fd in ready:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            with contextlib.suppress(OSError):
                sock.sendall(chunk)
        if sock_fd in ready:
            try:
                chunk = sock.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            with contextlib.suppress(OSError):
                os.write(master_fd, chunk)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bridge a real PTY to the pylon_stub.py TCP console.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--tcp-host", default="127.0.0.1", help="Stub address (default: 127.0.0.1)"
    )
    ap.add_argument(
        "--tcp-port",
        default=12300,
        type=int,
        help="Stub port to connect to; ignored with --spawn-stub (which "
        "always asks the OS for a free port — pass --port after -- to "
        "pin the spawned stub's port instead) (default: 12300)",
    )
    ap.add_argument(
        "--link",
        metavar="PATH",
        help="Also symlink the PTY slave here, e.g. /tmp/pylon-bms-tty, "
        "so SERIAL_PORT doesn't need updating every run",
    )
    ap.add_argument(
        "--spawn-stub",
        action="store_true",
        help="Launch scripts/pylon_stub.py instead of connecting to one "
        "already running; put its flags after --",
    )
    ap.add_argument(
        "stub_args",
        nargs=argparse.REMAINDER,
        help="With --spawn-stub: extra args forwarded to pylon_stub.py, e.g. "
        "-- --batteries 4 --model US5000",
    )
    args = ap.parse_args()

    stub_args = args.stub_args
    if stub_args and stub_args[0] == "--":
        stub_args = stub_args[1:]

    # Tracked outside the try so the finally below can clean up whatever
    # got acquired even if a later step (connect, openpty, symlink) fails
    # partway through — otherwise a spawned stub subprocess, in particular,
    # would leak as an orphan.
    stub_proc: subprocess.Popen[str] | None = None
    sock: socket.socket | None = None
    master_fd: int | None = None
    slave_fd: int | None = None
    link_path: Path | None = None
    try:
        tcp_port: int
        if args.spawn_stub:
            stub_proc, tcp_port = _spawn_stub(args.tcp_host, 0, stub_args)
        else:
            tcp_port = args.tcp_port

        sock = socket.create_connection((args.tcp_host, tcp_port))

        master_fd, slave_fd = os.openpty()
        slave_path = os.ttyname(slave_fd)
        # openpty()'s default line discipline is cooked, with echo on. Left
        # alone, every byte the bridge writes into master_fd (the stub's
        # own responses) would be echoed straight back into master_fd's
        # read queue and re-forwarded to the stub as if it were client
        # input — a feedback loop that garbles everything. Raw mode turns
        # that line discipline off so bytes pass through unmodified in
        # both directions, exactly like a real serial link.
        tty.setraw(slave_fd)
        # Keep our own fd on the slave open for the bridge's whole
        # lifetime — otherwise the kernel returns EIO from the master side
        # the instant nobody holds the slave open, i.e. the gap before a
        # client (screen, BmsConnection) opens it. See
        # tests/test_bms_connection_serial.py's serial_pty fixture, which
        # does the same thing for the same reason.

        if args.link:
            link_path = Path(args.link)
            with contextlib.suppress(FileNotFoundError):
                link_path.unlink()
            link_path.symlink_to(slave_path)

        print(f"[bridge] connected to stub at {args.tcp_host}:{tcp_port}")
        print(f"[bridge] PTY ready at {slave_path}")
        if link_path is not None:
            print(f"[bridge]   symlinked at {link_path}")
        shown_path = link_path if link_path is not None else slave_path
        print(f"[bridge]   point SERIAL_PORT there, or try: screen {shown_path} 115200")
        print("[bridge] Ctrl-C to stop")

        _bridge(master_fd, sock)
        print("[bridge] connection closed")
    except KeyboardInterrupt:
        print("\n[bridge] stopping")
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        for fd in (master_fd, slave_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        if link_path is not None:
            with contextlib.suppress(FileNotFoundError):
                link_path.unlink()
        if stub_proc is not None:
            stub_proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                stub_proc.wait(timeout=5)


if __name__ == "__main__":
    main()
