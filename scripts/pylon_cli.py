#!/usr/bin/env python3
"""
pylon_cli — interactive console client for a Pylontech BMS / scripts/pylon_stub.py
====================================================================================
Reuses src/main.py's BmsConnection — the same serial/TCP transport, response
framing and retry logic the sidecar itself runs — so this talks to the stub
(or real hardware) exactly the way the sidecar does, instead of a raw
telnet/nc session that doesn't know where one response ends and the next
begins.

.devcontainer/bin/pylon_cli (a symlink to this file) is on PATH inside the
devcontainer (see .devcontainer/postCreate.sh), so it can be run as a bare
`pylon_cli` from a terminal opened at the repo root.

Usage
-----
  # REPL against a running stub:
  python scripts/pylon_stub.py &
  pylon_cli --tcp 127.0.0.1:12300

  # One-shot command, no REPL — prints the response and exits:
  pylon_cli --tcp 127.0.0.1:12300 pwr 1

  # Serial (real hardware, or a scripts/pty_bridge.py PTY):
  pylon_cli --serial /dev/pts/7 --baud 115200

  # No connection flag: falls back to main.py's own environment variables
  # (CONNECTION_TYPE/TCP_HOST/TCP_PORT/SERIAL_PORT/BAUD_RATE), so an existing
  # sidecar .env just works unmodified.

In the REPL, type any BMS command (pwr, info, bat 1, login 000000, stub fault
1 ov, ...) and press Enter; Ctrl-D or 'exit'/'quit' ends the session. --raw
prints the exact bytes (repr) instead of normalized text — useful when
debugging the wire framing itself rather than a command's content.
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# .resolve() follows the .devcontainer/bin/pylon_cli symlink to this file's
# real location before walking up to the repo root — plain __file__ reflects
# however this was invoked (the symlink path, two directories deep, not one),
# which would otherwise point "src" at the wrong place entirely.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# No env-var ordering constraint (unlike `main`, imported later) -- this is
# a plain constant module, safe to import up front.
from parser_schema import CONSOLE_PROMPT  # noqa: E402

if TYPE_CHECKING:
    from main import BmsConnection

_CONSOLE_PROMPT_TEXT = CONSOLE_PROMPT.decode()


def _parse_host_port(value: str) -> tuple[str, int]:
    host, _, port_text = value.rpartition(":")
    if not host or not port_text.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 1-65535, got {port}")
    return host, port


def _clean(text: str) -> str:
    """Normalize a raw BMS response for terminal display.

    The wire protocol mixes bare \\r and \\n at framing boundaries (see
    pylon_stub.py's _wrap) rather than consistent \\r\\n — collapse any run of
    the two into a single newline. The trailing echoed CONSOLE_PROMPT
    (src/parser_schema.py -- the same constant src/main.py's own framing
    relies on, not a locally-guessed string) is dropped too since the REPL
    already prints its own next prompt.
    """
    text = re.sub(r"[\r\n]+", "\n", text).strip()
    if text.endswith(_CONSOLE_PROMPT_TEXT):
        text = text[: -len(_CONSOLE_PROMPT_TEXT)].rstrip("\n")
    return text


def _format(response: str, raw: bool) -> str:
    return repr(response) if raw else _clean(response)


def _send_or_report(bms: "BmsConnection", cmd: str) -> str | None:
    """Send cmd, returning its response, or None after printing a clean
    error and closing the connection.

    Catches OSError (covers ConnectionError/TimeoutError from the read
    path, plus serial.SerialException/socket.gaierror from the connect
    path -- all four are OSError subclasses in Python's exception
    hierarchy) and RuntimeError (send_command's own "socket/port is not
    open" guard clauses). Without closing on error, BmsConnection's
    _ensure_open only reopens when its transport handle is None (see
    src/main.py) -- leaving it as-is after a dropped connection means
    every subsequent command fails the same way until the process is
    restarted, with no way to recover short of that.
    """
    try:
        return bms.send_command(cmd)
    except (OSError, RuntimeError) as err:
        print(f"[pylon_cli] error: {err}", file=sys.stderr)
        bms.close()
        return None


def _run_repl(bms: "BmsConnection", raw: bool) -> None:
    print(
        "[pylon_cli] ready — type a command, or 'exit'/Ctrl-D to quit",
        file=sys.stderr,
    )
    while True:
        try:
            line = input("pylon> ")
        except EOFError:
            print(file=sys.stderr)
            return
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            return
        response = _send_or_report(bms, line)
        if response is not None:
            print(_format(response, raw))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Interactive console client for a Pylontech BMS / stub.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    connection = ap.add_mutually_exclusive_group()
    connection.add_argument(
        "--tcp", metavar="HOST:PORT", type=_parse_host_port, help="Connect over TCP"
    )
    connection.add_argument(
        "--serial", metavar="DEVICE", help="Connect over serial, e.g. /dev/ttyUSB0"
    )
    ap.add_argument(
        "--baud", type=int, default=115200, help="Serial baud rate (default: 115200)"
    )
    ap.add_argument(
        "--raw",
        action="store_true",
        help="Print exact bytes (repr) instead of normalized text",
    )
    ap.add_argument(
        "command",
        nargs="*",
        help="Send a single command and exit instead of starting the REPL",
    )
    args = ap.parse_args()

    if args.tcp:
        host, port = args.tcp
        os.environ["CONNECTION_TYPE"] = "tcp"
        os.environ["TCP_HOST"] = host
        os.environ["TCP_PORT"] = str(port)
    elif args.serial:
        os.environ["CONNECTION_TYPE"] = "serial"
        os.environ["SERIAL_PORT"] = args.serial
        os.environ["BAUD_RATE"] = str(args.baud)

    # Env vars above must land before this import — main.py reads its
    # CONNECTION_TYPE/TCP_*/SERIAL_PORT/BAUD_RATE config at module load time.
    import main as bms_main  # noqa: E402

    # main.py's own logging.basicConfig() points "pylon2mqtt" at stdout at
    # INFO level (every connect logs a line) — fine for the sidecar's own
    # logs, but here it would interleave with the command responses this
    # tool prints on the same stream. Warnings (e.g. a retried command)
    # still surface; routine connect/reconnect notices don't.
    logging.getLogger("pylon2mqtt").setLevel(logging.WARNING)

    bms = bms_main.BmsConnection()
    try:
        if args.command:
            cmd = " ".join(args.command)
            response = _send_or_report(bms, cmd)
            if response is None:
                sys.exit(1)
            print(_format(response, args.raw))
        else:
            _run_repl(bms, args.raw)
    finally:
        bms.close()


if __name__ == "__main__":
    main()
