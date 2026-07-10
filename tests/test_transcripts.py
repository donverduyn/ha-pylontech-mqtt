"""Replay real-hardware transcripts (tests/fixtures/transcripts/*.json)
through the real PylontechParser.

Every other parser test uses hand-built response strings written to match
our own understanding of the protocol (or scripts/pylon_stub.py, itself
hand-authored from the documented protocol) — none of them can catch a case
where real firmware simply doesn't behave the way the docs/stub assume.
This file has nothing to assert until someone contributes a transcript (see
that directory's README and scripts/capture_transcript.py); it exists so a
contributed one is exercised automatically with no wiring required.
"""

import json
import re
from pathlib import Path

import pytest

from pylontech_parser import PylontechParser
from structs import PylontechBattery, PylontechSystem

_TRANSCRIPT_DIR = Path(__file__).parent / "fixtures" / "transcripts"
_TRANSCRIPTS = sorted(_TRANSCRIPT_DIR.glob("*.json"))

_BATTERY_INDEX_RE = re.compile(r"^(pwr|bat) (\d+)$")


def _new_system() -> PylontechSystem:
    return PylontechSystem(0, 0, 0, 0, 0.0, 0.0, 0.0)


def _replay(transcript_path: Path) -> None:
    commands: dict[str, str] = json.loads(transcript_path.read_text())["commands"]
    system = _new_system()

    if "info" in commands:
        PylontechParser.parse_info(commands["info"], system)
    if "pwr" in commands:
        PylontechParser.parse_pwr(commands["pwr"], system)
    if "stat" in commands:
        PylontechParser.parse_stat(commands["stat"], system)
    if "time" in commands:
        PylontechParser.parse_time(commands["time"], system)

    for cmd, raw in commands.items():
        match = _BATTERY_INDEX_RE.match(cmd)
        if not match:
            continue
        kind, index_str = match.groups()
        bat_id = int(index_str)
        if kind == "pwr":
            PylontechParser.parse_pwr_indexed(raw, bat_id)
        else:
            existing = next((b for b in system.batteries if b.sys_id == bat_id), None)
            battery = existing or PylontechBattery(
                bat_id, 0.0, 0.0, 0.0, 0, "", 0.0, 0.0
            )
            PylontechParser.parse_bat(raw, battery)

    if "pwr" in commands:
        assert system.voltage >= 0


if _TRANSCRIPTS:

    @pytest.mark.parametrize(
        "transcript_path", _TRANSCRIPTS, ids=[p.stem for p in _TRANSCRIPTS]
    )
    def test_transcript_replays_without_error(transcript_path: Path) -> None:
        _replay(transcript_path)

# No placeholder test when _TRANSCRIPTS is empty: defining one just to skip
# it still prints a SKIPPED line under this repo's `-v` addopts. Omitting the
# test entirely means pytest collects zero items from this file — silent
# until a transcript is contributed.
