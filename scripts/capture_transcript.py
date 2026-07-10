#!/usr/bin/env python3
"""Capture a real BMS console transcript for the fixtures corpus.

The test suite's BMS stub (scripts/pylon_stub.py) is hand-authored from the
documented protocol, not recorded from real hardware — there is currently no
checked-in corpus of actual US2000/3000/5000 (or Pytes-branded) firmware
responses. This script connects to a real BMS exactly the way src/main.py
does (same env vars, same BmsConnection) and records the raw response to
every command the sidecar issues, so a contributor with real hardware can
submit one as tests/fixtures/transcripts/<name>.json.

Usage
-----
  # Serial (defaults, same as src/main.py):
  python scripts/capture_transcript.py --out my_us3000_transcript.json

  # TCP:
  CONNECTION_TYPE=tcp TCP_HOST=192.168.1.50 \\
      python scripts/capture_transcript.py --battery-count 4 --out my_stack.json

Before submitting, open the output file and check the --redact pass actually
caught everything specific to your installation — it only targets the one
field (Barcode) the codebase already treats as identifying (see
diagnostics.py's TO_REDACT), not a guarantee. When in doubt, hand-edit or ask
before opening a PR with it.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import main  # noqa: E402  (must follow the sys.path insert above)

_REDACTED = "[REDACTED]"

# Only "Barcode" — the one info-command field diagnostics.py's own
# TO_REDACT set treats as unique per physical device. A blanket "redact any
# long alphanumeric token" pattern was tried and rejected: fields like
# "Total Power In"/"Pwr Coulomb" are long digit runs glued directly to a
# unit suffix (e.g. "153311400AS"), indistinguishable by shape alone from an
# actual serial, and got wrongly stripped. "Board version" is a firmware
# revision shared by every unit of a model, not unique either — left alone
# deliberately.
_BARCODE_LINE_RE = re.compile(r"(Barcode\s*:\s*)(\S+)")


def _redact(text: str) -> str:
    return _BARCODE_LINE_RE.sub(rf"\1{_REDACTED}", text)


def capture(battery_count: int, redact: bool) -> dict[str, Any]:
    bms = main.BmsConnection()
    try:
        commands = ["info", "pwr", "stat", "time"]
        commands += [f"pwr {n}" for n in range(1, battery_count + 1)]
        commands += [f"bat {n}" for n in range(1, battery_count + 1)]

        transcript: dict[str, str] = {}
        for cmd in commands:
            print(f"Sending {cmd!r}...", file=sys.stderr)
            try:
                response = bms.send_command(cmd)
            except Exception as err:  # noqa: BLE001 - record failures too
                response = f"<error: {err}>"
            transcript[cmd] = _redact(response) if redact else response
    finally:
        bms.close()

    return {
        "connection_type": main.CONNECTION_TYPE,
        "monitoring_level": main.MONITORING_LEVEL,
        "commands": transcript,
    }


def main_cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--battery-count",
        type=int,
        default=1,
        help="Number of 'pwr N'/'bat N' probes to send (default: 1)",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output path, written under tests/fixtures/transcripts/",
    )
    parser.add_argument(
        "--no-redact",
        action="store_true",
        help="Skip the best-effort serial/barcode redaction pass",
    )
    args = parser.parse_args()

    result = capture(args.battery_count, redact=not args.no_redact)

    out_dir = Path(__file__).parent.parent / "tests" / "fixtures" / "transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / args.out
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")

    print(f"Wrote {out_path}", file=sys.stderr)
    print(
        "Review it by hand before opening a PR — redaction is best-effort, "
        "not a guarantee.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main_cli()
