"""Pylontech-protocol schema instances for src/parser.py's generic engine,
plus the BMS console command strings themselves (CMD_* / cmd_*).

Every Pylontech-specific string lives here, in data: column names, key
labels, response-parsing schemas, and also the command names/formats the
sidecar sends over the wire — src/main.py's poll loop decides *when* to send
each one, but not what it's spelled as. Neither this module nor src/main.py
knows the other's business beyond that: this module never imports
src/main.py, and src/parser.py never imports this module. main.py is the one
place that imports both, wiring a schema from here to a
``parser.Parser(SCHEMA)`` instance for each command it sends.

The row_factory/aggregate functions below (e.g. _build_battery_from_pwr_row)
do reference structs.py's dataclasses, since building the final output
object is exactly what a schema is responsible for — but they contain no
parsing logic themselves, only field-by-field construction from values the
engine already extracted.
"""

import re
from datetime import datetime
from typing import Any

from parser import (
    ColumnSpec,
    KeyValueSchema,
    KVField,
    LooseKeyField,
    LooseKeySchema,
    RegexField,
    TableSchema,
    optional_int_by_bounds,
    optional_milli,
    optional_status,
    optional_str_by_bounds,
    parse_number,
    percent_int_or_zero,
    required_int,
    required_milli,
    required_percent_int,
    required_str,
)
from structs import PylontechBattery, PylontechCell, PylontechSystem

# ---------------------------------------------------------------------------
# 'pwr' aggregate table
# ---------------------------------------------------------------------------


def _build_battery_from_pwr_row(v: dict[str, Any]) -> PylontechBattery:
    return PylontechBattery(
        sys_id=v["sys_id"],
        voltage=v["voltage"],
        current=v["current"],
        temperature=v["temperature"],
        soc=v["soc"],
        status=v["status"],
        power=round(v["voltage"] * v["current"], 2),
        energy_stored=0.0,
        temp_low=v.get("temp_low"),
        temp_high=v.get("temp_high"),
        volt_low=v.get("volt_low"),
        volt_high=v.get("volt_high"),
        volt_status=v.get("volt_status"),
        curr_status=v.get("curr_status"),
        temp_status=v.get("temp_status"),
        batt_volt_status=v.get("batt_volt_status"),
        batt_temp_status=v.get("batt_temp_status"),
    )


def _aggregate_pwr(batteries: list[PylontechBattery], system: PylontechSystem) -> None:
    system.batteries = batteries
    if batteries:
        system.voltage = round(sum(b.voltage for b in batteries) / len(batteries), 2)
        system.current = round(sum(b.current for b in batteries), 2)
        system.soc = round(sum(b.soc for b in batteries) / len(batteries), 1)
        system.power = round(sum(b.power for b in batteries), 1)
    else:
        system.voltage = 0.0
        system.current = 0.0
        system.soc = 0.0
        system.power = 0.0


PWR_TABLE_SCHEMA = TableSchema(
    header_first_token="Power",
    header_must_contain="Coulomb",
    row_error_label="pwr",
    is_data_row=lambda parts: len(parts) > 10 and parts[0].isdigit(),
    row_factory=_build_battery_from_pwr_row,
    skip_row=lambda line: "Absent" in line,
    aggregate=_aggregate_pwr,
    columns=[
        ColumnSpec(("Power",), field="sys_id", default_index=0, transform=required_int),
        ColumnSpec(
            ("Volt",), field="voltage", default_index=1, transform=required_milli
        ),
        ColumnSpec(
            ("Curr",), field="current", default_index=2, transform=required_milli
        ),
        ColumnSpec(
            ("Tempr",), field="temperature", default_index=3, transform=required_milli
        ),
        ColumnSpec(
            ("Tlow",), field="temp_low", default_index=4, transform=optional_milli
        ),
        ColumnSpec(
            ("Thigh",), field="temp_high", default_index=5, transform=optional_milli
        ),
        ColumnSpec(
            ("Vlow",), field="volt_low", default_index=6, transform=optional_milli
        ),
        ColumnSpec(
            ("Vhigh",), field="volt_high", default_index=7, transform=optional_milli
        ),
        ColumnSpec(
            ("Base.St",), field="status", default_index=8, transform=required_str
        ),
        ColumnSpec(
            ("Volt.St",),
            field="volt_status",
            default_index=9,
            transform=optional_status,
        ),
        ColumnSpec(
            ("Curr.St",),
            field="curr_status",
            default_index=10,
            transform=optional_status,
        ),
        ColumnSpec(
            ("Temp.St",),
            field="temp_status",
            default_index=11,
            transform=optional_status,
        ),
        ColumnSpec(
            ("Coulomb",), field="soc", default_index=12, transform=required_percent_int
        ),
        # "Time" expands to two data tokens (date + clock); it carries no
        # field of its own, but its width must be counted so every column
        # after it (B.V.St, B.T.St) resolves to the right data index.
        ColumnSpec(("Time",), data_width=2),
        ColumnSpec(
            ("B.V.St",),
            field="batt_volt_status",
            default_index=15,
            transform=optional_status,
        ),
        ColumnSpec(
            ("B.T.St",),
            field="batt_temp_status",
            default_index=16,
            transform=optional_status,
        ),
    ],
)


# ---------------------------------------------------------------------------
# 'bat N' per-cell table
# ---------------------------------------------------------------------------


def _build_cell_from_bat_row(v: dict[str, Any]) -> PylontechCell:
    return PylontechCell(
        cell_id=v["cell_id"],
        voltage=v["voltage"],
        current=v["current"],
        temperature=v["temperature"],
        base_state=v["base_state"],
        volt_status=v.get("volt_status"),
        curr_status=v.get("curr_status"),
        temp_status=v.get("temp_status"),
        soc=v.get("soc", 0),
        capacity=v.get("capacity"),
    )


def _bat_header_postprocess(indices: dict[str, int], seen: set[str]) -> dict[str, int]:
    """Some Pytes/Pylontech firmware omits the "SOC" header label even
    though each data row still carries a percentage token immediately before
    the Coulomb (mAh) value — shift capacity's index down to make room."""
    if "SOC" not in seen:
        indices["soc"] = indices["capacity"]
        indices["capacity"] = indices["capacity"] + 1
    return indices


def _assign_cells(cells: list[PylontechCell], battery: PylontechBattery) -> None:
    battery.cells = cells


BAT_TABLE_SCHEMA = TableSchema(
    header_first_token="Battery",
    header_must_contain="Coulomb",
    row_error_label="bat",
    is_data_row=lambda parts: bool(parts) and parts[0].isdigit(),
    row_factory=_build_cell_from_bat_row,
    min_index_field="soc",
    header_postprocess=_bat_header_postprocess,
    aggregate=_assign_cells,
    columns=[
        ColumnSpec(
            ("Battery",), field="cell_id", default_index=0, transform=required_int
        ),
        ColumnSpec(
            ("Volt",), field="voltage", default_index=1, transform=required_milli
        ),
        ColumnSpec(
            ("Curr",), field="current", default_index=2, transform=required_milli
        ),
        ColumnSpec(
            ("Tempr",), field="temperature", default_index=3, transform=required_milli
        ),
        ColumnSpec(
            ("Base", "State"),
            field="base_state",
            default_index=4,
            transform=required_str,
        ),
        ColumnSpec(
            ("Volt.", "State"),
            field="volt_status",
            default_index=5,
            transform=optional_str_by_bounds,
        ),
        ColumnSpec(
            ("Curr.", "State"),
            field="curr_status",
            default_index=6,
            transform=optional_str_by_bounds,
        ),
        ColumnSpec(
            ("Temp.", "State"),
            field="temp_status",
            default_index=7,
            transform=optional_str_by_bounds,
        ),
        ColumnSpec(
            ("SOC",), field="soc", default_index=8, transform=percent_int_or_zero
        ),
        ColumnSpec(
            ("Coulomb",),
            field="capacity",
            default_index=9,
            transform=optional_int_by_bounds,
        ),
    ],
)


# ---------------------------------------------------------------------------
# 'pwr N' vertical key:value block
# ---------------------------------------------------------------------------


def _build_battery_from_pwr_indexed(v: dict[str, Any]) -> PylontechBattery:
    return PylontechBattery(
        sys_id=v["sys_id"],
        voltage=v["voltage"],
        current=v["current"],
        temperature=v["temperature"],
        soc=v["soc"],
        status=v["status"],
        power=round(v["voltage"] * v["current"], 2),
        energy_stored=0.0,
        volt_status=v["volt_status"],
        curr_status=v["curr_status"],
        temp_status=v["temp_status"],
        coul_status=v["coul_status"],
        bat_events=v["bat_events"],
        power_events=v["power_events"],
        sys_fault=v["sys_fault"],
    )


PWR_INDEXED_SCHEMA = KeyValueSchema(
    not_found_marker="not found",
    invalid_if=lambda fields: fields.get("Basic Status", "").lower() == "absent",
    row_factory=_build_battery_from_pwr_indexed,
    fields=[
        KVField("Voltage", "voltage", lambda s: int(s) / 1000.0, required=True),
        KVField("Current", "current", lambda s: int(s) / 1000.0, required=True),
        KVField("Temperature", "temperature", lambda s: int(s) / 1000.0, required=True),
        KVField("Coulomb", "soc", lambda s: int(s.replace("%", "")), required=True),
        KVField("Basic Status", "status", default=""),
        KVField("Volt Status", "volt_status"),
        KVField("Current Status", "curr_status"),
        KVField("Tmpr. Status", "temp_status"),
        KVField("Coul. Status", "coul_status"),
        KVField("Bat Events", "bat_events", parse_number),
        KVField("Power Events", "power_events", parse_number),
        KVField("System Fault", "sys_fault", parse_number),
    ],
)


# ---------------------------------------------------------------------------
# 'info' loosely-keyed dump
# ---------------------------------------------------------------------------


def _max_dischg_curr(val: str) -> float:
    return abs(int(re.sub(r"[^\d-]", "", val))) / 1000.0


def _max_charge_curr(val: str) -> float:
    return int(re.sub(r"\D", "", val)) / 1000.0


INFO_SCHEMA = LooseKeySchema(
    fields=[
        LooseKeyField(lambda k: "manufacturer" in k, "manufacturer"),
        LooseKeyField(lambda k: "device name" in k, "model"),
        LooseKeyField(lambda k: "main soft" in k, "fw_version"),
        LooseKeyField(lambda k: "board version" in k, "board_version"),
        LooseKeyField(lambda k: k == "soft version", "soft_version"),
        LooseKeyField(lambda k: k == "boot version", "boot_version"),
        LooseKeyField(lambda k: "comm version" in k, "comm_version"),
        LooseKeyField(lambda k: "release date" in k, "release_date"),
        LooseKeyField(lambda k: "barcode" in k, "barcode"),
        LooseKeyField(lambda k: "specification" in k, "spec"),
        LooseKeyField(lambda k: "cell number" in k, "cell_count", int),
        LooseKeyField(
            lambda k: "max dischg curr" in k, "max_dischg_curr", _max_dischg_curr
        ),
        LooseKeyField(
            lambda k: "max charge curr" in k, "max_charge_curr", _max_charge_curr
        ),
    ]
)


# ---------------------------------------------------------------------------
# 'stat' regex counters + 'time' regex stamp
# ---------------------------------------------------------------------------

STAT_FIELDS = [
    RegexField(r"Sys SOH\s*:\s*(\d+)", "soh", int),
    RegexField(r"CYCLE Times\s*:\s*(\d+)", "cycles", int),
    RegexField(r"Charge Times\s*:\s*(\d+)", "charge_times", int),
    RegexField(r"Discharge Cnt\.\s*:\s*(\d+)", "discharge_cnt", int),
    RegexField(r"Idle Times\s*:\s*(\d+)", "idle_times", int),
    RegexField(r"Shut Times\s*:\s*(\d+)", "shut_times", int),
    RegexField(r"Reset Times\s*:\s*(\d+)", "reset_times", int),
    RegexField(r"SC Times\s*:\s*(\d+)", "sc_times", int),
    RegexField(r"Bat OV Times\s*:\s*(\d+)", "bat_ov_times", int),
    RegexField(r"Bat HV Times\s*:\s*(\d+)", "bat_hv_times", int),
    RegexField(r"Bat LV Times\s*:\s*(\d+)", "bat_lv_times", int),
    RegexField(r"Bat UV Times\s*:\s*(\d+)", "bat_uv_times", int),
    RegexField(r"Pwr OV Times\s*:\s*(\d+)", "pwr_ov_times", int),
    RegexField(r"Pwr HV Times\s*:\s*(\d+)", "pwr_hv_times", int),
    RegexField(r"LifeWarn Times\s*:\s*(\d+)", "life_warn_times", int),
    RegexField(r"LifeAlarm Times\s*:\s*(\d+)", "life_alarm_times", int),
    RegexField(r"Pwr Coulomb\s*:\s*(\d+)", "pwr_coulomb", int),
    RegexField(r"Dsg Cap\s*:\s*(\d+)", "dsg_cap", int),
]

# Unlike stat's counters, a miss must leave any previously-known bms_time
# alone rather than clobbering it with None — the BMS clock is still
# whatever it last reported.
TIME_FIELDS = [
    RegexField(r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})", "bms_time", always_set=False),
]

# "time %y %m %d %H %M %S" — the console's clock-set command; not a parser,
# but a Pylontech-protocol string that belongs here rather than in main.py.
TIME_COMMAND_FORMAT = "time %y %m %d %H %M %S"


def generate_time_command(timestamp: datetime) -> str:
    """Build the 'time' command that sets the BMS console's clock to
    *timestamp* (see main.py's AUTO_SYNC_TIME)."""
    return timestamp.strftime(TIME_COMMAND_FORMAT)


# ---------------------------------------------------------------------------
# BMS console command names — what the poll loop sends, not how it decides
# when to send it. Paired with the schema each one's response is parsed
# against: CMD_INFO -> INFO_SCHEMA, CMD_PWR -> PWR_TABLE_SCHEMA,
# cmd_pwr_indexed() -> PWR_INDEXED_SCHEMA, CMD_STAT -> STAT_FIELDS,
# CMD_TIME -> TIME_FIELDS, cmd_bat() -> BAT_TABLE_SCHEMA.
# ---------------------------------------------------------------------------

CMD_INFO = "info"
CMD_PWR = "pwr"
CMD_STAT = "stat"
CMD_TIME = "time"


def cmd_pwr_indexed(bat_id: int) -> str:
    """The per-slot vertical-block variant of 'pwr' (see PWR_INDEXED_SCHEMA)."""
    return f"pwr {bat_id}"


def cmd_bat(bat_id: int) -> str:
    """Per-cell detail for one battery (see BAT_TABLE_SCHEMA)."""
    return f"bat {bat_id}"


# ---------------------------------------------------------------------------
# Console transport framing — how the Pylontech console marks a response as
# complete. src/main.py's BmsConnection owns *how* it waits (polling,
# timeouts, retries, the grace period after a terminator) but not what the
# terminator bytes themselves are, which is protocol identity, not
# connection-handling behavior.
# ---------------------------------------------------------------------------

CONSOLE_PROMPT = b"pylon>"
# Some Pytes/Pylontech firmware variants complete a command without ever
# emitting the "pylon>" prompt. Accepting this alternate terminator too keeps
# those consoles from timing out on every single command.
CONSOLE_ALT_TERMINATOR = b"Command completed"
