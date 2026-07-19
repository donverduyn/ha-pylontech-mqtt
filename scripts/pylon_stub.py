#!/usr/bin/env python3
"""
Pylontech BMS RS232 → TCP Stub Server
======================================
Emulates the Pylontech RS232 console protocol over a raw TCP socket.

Usage
-----
  python pylon_stub.py                                 # 2× US2000, new fw, port 12300
  python pylon_stub.py --batteries 8 --model US5000    # 8× US5000
  python pylon_stub.py --groups 6 --batteries 16       # 6-group LV-HUB, 96 modules
  python pylon_stub.py --firmware old                  # pre-2024 column layout
  python pylon_stub.py --fw-version B84.5              # 'info' Main Soft version
  python pylon_stub.py --port 9999 --host 0.0.0.0

Implements
----------
  pwr [N]          power table (or single-battery view when N given)
  info             device information
  stat             statistics (fault counters, coulombs …)
  bat [N]          per-cell data for one module
  soh [N]          per-cell SOH data for one module
  time             read / set BMS clock
  re <addr> <cmd>  forward any command to a remote battery
  login [pw]       enter admin mode (password: 000000)
  logout           return to user mode
  log              event log (abbreviated stub)
  data             history data (stub)
  datalist         historical samples (stub)
  disp             single-shot pwr snapshot (streaming not emulated)
  getpwr           alias for pwr
  help             paginated command list
  shut/trst/updata stubs
  stub [...]       runtime control: fault inject, soc, current (admin mode)

Firmware variants (--firmware; column layout only)
---------------------------------------------------
  old   16 columns — Volt … B.T.St  (pre-Tlow.Id era; matches the real
        US2KBPL capture in docs/docs.md verbatim, incl. the "bat" command's
        missing SOC header)
  new   23 columns — adds *.Id columns, MosTempr/M.T.St and SysAlarm.St
        (default)

'info's reported firmware string (--fw-version, default B66.6) is
independent of --firmware — it doesn't change any column layout, only the
"Main Soft version" value a client reads back.

Admin control (after ``login 000000``)
---------------------------------------
  stub fault <bat> <ov|uv|ot|ut|oc|absent>  inject fault; affects all views
  stub clear <bat>                           remove injected fault
  stub soc <pct>                             set SOC percentage
  stub current <mA>                          fix current (persists across ticks)
  stub current auto                          return current to updater control
"""

import argparse
import contextlib
import datetime
import random
import socketserver
import threading
import time
from typing import TypedDict


class _ModelSpec(TypedDict):
    device_name: str
    spec: str
    cells: int
    cap_mah: int
    max_chg: int
    max_dsg: int


class _Config(TypedDict):
    model: str
    batteries: int  # present modules per group
    slots: int  # total pwr rows per group  (slots >= batteries)
    groups: int  # number of parallel groups (LV-HUB)
    firmware: str  # "old" (16-col) or "new" (23-col with *.Id)
    fw_version: str  # reported as 'info's "Main Soft version" field
    tick_interval: int  # seconds between simulated state ticks


class _State(TypedDict):
    soc: int
    charging: bool
    voltage: int  # mV  (stack voltage)
    current: int  # mA  (positive = charging)
    temperature: int  # mC  (pack temperature)
    temp_low: int  # mC  min cell temperature
    temp_high: int  # mC  max cell temperature
    volt_low: int  # mV  min cell voltage
    volt_high: int  # mV  max cell voltage
    mostempr: int  # mC  MOSFET temperature
    cycles: int
    charge_times: int
    discharge_cnt: int
    idle_times: int
    shut_times: int
    reset_times: int
    sc_times: int
    bat_ov_times: int
    bat_hv_times: int
    bat_lv_times: int
    bat_uv_times: int
    pwr_ov_times: int
    pwr_hv_times: int
    life_warn_times: int
    life_alarm_times: int
    pwr_coulomb: int
    dsg_cap: int
    charge_cnt: int  # finer-grained than charge_times — ticks while charging
    current_override: (
        int | None
    )  # mA; when set the updater skips current; clear with 'stub current auto'


# Model catalogue
MODELS: dict[str, _ModelSpec] = {
    "US2000": {
        "device_name": "US2KBPL",
        "spec": "48V/50AH",
        "cells": 15,
        "cap_mah": 50000,
        "max_chg": 102000,
        "max_dsg": -100000,
    },
    "US3000": {
        "device_name": "US3KBPL",
        "spec": "48V/74AH",
        "cells": 15,
        "cap_mah": 74000,
        "max_chg": 150000,
        "max_dsg": -150000,
    },
    "US5000": {
        "device_name": "US5KBPL",
        "spec": "48V/100AH",
        "cells": 15,
        "cap_mah": 100000,
        "max_chg": 200000,
        "max_dsg": -200000,
    },
}

# Runtime configuration  (filled by main() before server starts)
_cfg: _Config = {
    "model": "US2000",
    "batteries": 2,
    "slots": 8,
    "groups": 1,
    "firmware": "new",
    "fw_version": "B66.6",
    "tick_interval": 30,
}

# Shared BMS state
_state: _State = {
    "soc": 85,
    "charging": True,
    "voltage": 50691,
    "current": 3806,
    "temperature": 17000,
    "temp_low": 15000,
    "temp_high": 19000,
    "volt_low": 3378,
    "volt_high": 3381,
    "mostempr": 22700,
    "cycles": 430,
    "charge_times": 1150,
    "discharge_cnt": 0,
    "idle_times": 23858,
    "shut_times": 329,
    "reset_times": 67,
    "sc_times": 0,
    "bat_ov_times": 56,
    "bat_hv_times": 5832,
    "bat_lv_times": 0,
    "bat_uv_times": 0,
    "pwr_ov_times": 4688,
    "pwr_hv_times": 6734,
    "life_warn_times": 0,
    "life_alarm_times": 0,
    "pwr_coulomb": 153311400,
    "dsg_cap": 21506462,
    "charge_cnt": 4681,  # docs/docs.md's real capture: 4681 vs Charge Times 1150
    "current_override": None,
}
_state_lock = threading.Lock()
_admin_mode = False  # toggled by login/logout
_faults: dict[
    int, str
] = {}  # bat_id → fault type; populated by 'stub fault', cleared by 'stub clear'


# Background state updater
def _state_updater() -> None:
    while True:
        time.sleep(_cfg["tick_interval"])
        with _state_lock:
            s = _state
            if s["current_override"] is None:
                if s["charging"]:
                    s["soc"] = min(100, s["soc"] + 1)
                    s["current"] = random.randint(3000, 5000)
                    if s["soc"] >= 100:
                        s["charging"] = False
                        s["current"] = -random.randint(1000, 3000)
                        s["discharge_cnt"] += 1
                else:
                    s["soc"] = max(0, s["soc"] - 1)
                    s["current"] = -random.randint(1000, 4000)
                    if s["soc"] <= 10:
                        s["charging"] = True
                        s["current"] = random.randint(3000, 5000)
                        s["cycles"] += 1
                        s["charge_times"] += 1
            else:
                s["current"] = s["current_override"]
                s["charging"] = s["current_override"] > 0
            if s["charging"]:
                s["charge_cnt"] += 1
            base_mv = 3000 + int(s["soc"] * 6.5)
            s["volt_low"] = base_mv + random.randint(0, 3)
            s["volt_high"] = s["volt_low"] + random.randint(1, 8)
            s["voltage"] = s["volt_low"] * 15
            # Temperature drift — preserve Tlow ≤ Tempr ≤ Thigh invariant
            s["temperature"] = max(
                10000, min(45000, s["temperature"] + random.randint(-200, 200))
            )
            s["temp_low"] = s["temperature"] - random.randint(1000, 3000)
            s["temp_high"] = s["temperature"] + random.randint(500, 2000)
            s["mostempr"] = s["temperature"] + random.randint(1000, 3000)
            # Accumulate lifetime energy counters (mA × tick_interval s ÷ 1000 → A·s)
            amp_secs = abs(s["current"]) * _cfg["tick_interval"] // 1000
            if s["current"] > 0:
                s["pwr_coulomb"] += amp_secs
            else:
                s["dsg_cap"] += amp_secs


# Response envelope
_PROMPT = b"\r\npylon>"


def _wrap(cmd_echo: str, body: str, kv: bool = False) -> bytes:
    after = "\n\r" if kv else "\r\n"
    text = (
        f"{cmd_echo}\n\r@\r{after}{body}\r\n\r"
        f"Command completed successfully\r\n\r$$\r\n\rpylon>"
    )
    return text.encode("ascii", errors="replace")


def _unknown(cmd: str) -> bytes:
    base = cmd.split()[0] if cmd.split() else cmd
    return f"{cmd}\r\nUnknown command '{base}'\r\n\r$$\r\n\rpylon>".encode("ascii")


def _parse_bat_id_arg(cmd: str, default: int = 1) -> int:
    """Parse an optional battery-id argument from a command like 'bat 3'."""
    parts = cmd.split()
    if len(parts) > 1:
        with contextlib.suppress(ValueError):
            return int(parts[1])
    return default


def _battery_slot_status(bat_id: int) -> tuple[bool, str | None]:
    """Return (present, fault) for bat_id under the current group/slot config."""
    slots_per_group = _cfg["slots"]
    batt_per_group = _cfg["batteries"]
    slot_in_group = ((bat_id - 1) % slots_per_group) + 1
    present = slot_in_group <= batt_per_group
    return present, _faults.get(bat_id)


# pwr N (indexed) — vertical per-battery key:value block
# This is a completely different response shape from the tabular `pwr` output.
# Real firmware returns this block when an index argument is given:
#
#   ----------------------------
#   Power N
#   Voltage         : 52713  mV
#   Current         : 3806   mA
#   ...
#   ----------------------------
#
# Clients such as pytes_serial.py call `pwr N` exclusively and parse the
# response with fixed byte offsets:
#   line_str[1:18]  — key field (17 chars incl. colon; pos 0 is \r from framing)
#   line_str[19:27] — value field (8 chars, whitespace-padded)
#
# The \r at position 0 arrives automatically from the \r\n\r separator that
# _wrap(kv=True) inserts between body lines — so body lines are formatted
# WITHOUT a leading \r here.
def _resp_pwr_indexed(cmd: str, bat_id: int) -> bytes:
    """Return the vertical key:value block for battery `bat_id`."""
    with _state_lock:
        s = _state.copy()

    n_groups = _cfg["groups"]
    slots_per_group = _cfg["slots"]
    m = MODELS[_cfg["model"]]

    if bat_id < 1 or bat_id > n_groups * slots_per_group:
        return _wrap(cmd, f"Power {bat_id} not found", kv=True)

    present, fault = _battery_slot_status(bat_id)

    SEP = "----------------------------"

    def kv(key: str, value: object, unit: str = "") -> str:
        # Produces: '{key:<16}: {value:<8}{unit}'
        # With the leading \r from framing:
        #   line_str[1:18] == f'{key:<16}:'   ← key check
        #   line_str[19:27] == f'{value:<8}'  ← 8-char value field
        return f"{key:<16}: {str(value):<8}{unit}"

    if not present or fault == "absent":
        lines = [SEP, f"Power {bat_id}", kv("Basic Status", "Absent"), SEP]
    else:
        base_st = _base_state(s["current"])
        # CMOS/DMOS: both ON in normal operation; protection-specific FET opens on fault
        if fault == "ov":
            cmos_st, dmos_st = "OFF", "ON"  # OV: block further charging
        elif fault in ("uv", "ut"):
            cmos_st, dmos_st = "ON", "OFF"  # UV/UT: block discharging
        elif fault in ("ot", "oc"):
            cmos_st, dmos_st = "OFF", "OFF"  # OT/OC: full protection
        else:
            cmos_st, dmos_st = "ON", "ON"  # Normal: both FETs conducting
        real_cap = int(m["cap_mah"] * _soh_pct(s["cycles"]) / 100)
        volt_st = "OV" if fault == "ov" else "UV" if fault == "uv" else "Normal"
        curr_st = "OC" if fault == "oc" else "Normal"
        temp_st = "OT" if fault == "ot" else "UT" if fault == "ut" else "Normal"
        bat_st = (
            "VOV TNOR"
            if fault == "ov"
            else "VUV TNOR"
            if fault == "uv"
            else "VNOR TOT"
            if fault == "ot"
            else "VNOR TUT"
            if fault == "ut"
            else "VNOR TNOR"
        )
        # Event bitmasks — non-zero when a fault is active
        bat_events = (
            "0x1"
            if fault == "ov"
            else "0x4"
            if fault == "uv"
            else "0x10"
            if fault == "ot"
            else "0x40"
            if fault == "ut"
            else "0x0"
        )
        pwr_events = "0x1" if fault == "oc" else "0x0"
        sys_fault_bits = "0x1" if fault and fault != "absent" else "0x0"
        # Raw values pushed past threshold so telemetry-driven monitors also fire
        disp_voltage = s["voltage"]
        disp_current = s["current"]
        disp_temp = s["temperature"]
        if fault == "ov":
            disp_voltage = 3870 * m["cells"]  # ~3.87 V/cell, above typical OV threshold
        elif fault == "uv":
            disp_voltage = 2750 * m["cells"]  # ~2.75 V/cell, below typical UV threshold
        elif fault == "ot":
            disp_temp = 55000  # 55 °C, above typical OT threshold
        elif fault == "ut":
            disp_temp = -10000  # -10 °C, below typical UT threshold
        elif fault == "oc":
            disp_current = (
                -150000 if s["current"] < 0 else 150000
            )  # fits 8-char kv field
        lines = [
            SEP,
            f"Power {bat_id}",
            kv("Voltage", disp_voltage, "mV"),
            kv("SOC Voltage", "0", "mV"),
            kv("Current", disp_current, "mA"),
            kv("Temperature", disp_temp, "mC"),
            kv("Coulomb", s["soc"], "%"),
            kv("Total Coulomb", m["cap_mah"], "mAH"),
            kv("Real Coulomb", real_cap, "mAH"),
            kv("Total Power In", s["pwr_coulomb"], "AS"),
            kv("Total Power Out", s["dsg_cap"], "AS"),
            kv("Basic Status", base_st),
            kv("Volt Status", volt_st),
            kv("Current Status", curr_st),
            kv("Tmpr. Status", temp_st),
            kv("Coul. Status", "Normal"),
            kv("Bat Status", bat_st),
            kv("CMOS Status", cmos_st),
            kv("DMOS Status", dmos_st),
            # Protection enable masks — value exceeds 8 chars; formatted directly.
            f"{'Bat Protect ENA':<16}: OV HV LV UV SLP OT HT LT UT",
            f"{'Pwr Protect ENA':<16}: OV HV LV UV SLP OT HT LT UT "
            "COC COC2 COCA DOCA DOC DOC2 SC",
            kv("Bat Events", bat_events),
            kv("Power Events", pwr_events),
            kv("System Fault", sys_fault_bits),
            kv("COMM EX Status", "0x0"),
            SEP,
        ]

    body = "\r\n\r".join(lines)
    return _wrap(cmd, body, kv=True)


# pwr
def _base_state(current_ma: int) -> str:
    if abs(current_ma) < 500:
        return "Idle"
    return "Charge" if current_ma > 0 else "Dischg"


def _soh_pct(cycles: int) -> int:
    """SOH degrades ~0.02 percentage points per charge cycle, floored at 0%.

    Shared by every view that derives capacity/health from cycle count (pwr
    N's Real Coulomb, stat/soh/pwrsys's Sys SOH, bat's per-cell capacity) so
    they always agree with each other as the simulated pack ages."""
    return max(0, 100 - int(cycles * 0.02))


def _resp_pwr(cmd: str) -> bytes:
    with _state_lock:
        s = _state.copy()

    batt_per_group = _cfg["batteries"]
    slots_per_group = _cfg["slots"]
    n_groups = _cfg["groups"]
    firmware = _cfg["firmware"]
    cells = MODELS[_cfg["model"]]["cells"]

    # pwr [index] — if N given, show only the single row for sequential battery
    # ID N.  On real hardware `pwr` (no arg) lists every slot; `pwr N` returns
    # exactly one row (the slot whose sequential bat_id == N).
    parts = cmd.split()
    bat_id_filter: int | None = None
    if len(parts) > 1:
        with contextlib.suppress(ValueError):
            bat_id_filter = int(parts[1])

    # pwr N → vertical per-battery block (completely different format from table)
    if bat_id_filter is not None:
        return _resp_pwr_indexed(cmd, bat_id_filter)

    # pwr (no arg) → multi-row ASCII table
    base_st = _base_state(s["current"])
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if firmware == "new":
        header = (
            "Power Volt   Curr   Tempr  Tlow   Tlow.Id Thigh  Thigh.Id Vlow   Vlow.Id  "
            "Vhigh  Vhigh.Id Base.St  Volt.St  Curr.St  Temp.St  Coulomb  "
            "Time                 B.V.St   B.T.St   MosTempr  M.T.St   SysAlarm.St"
        )
    else:
        # Matches docs/docs.md's real US2KBPL capture verbatim (16 header
        # tokens / 17 data tokens incl. the 2-token Time field) — that
        # hardware has no MosTempr/M.T.St columns at all; those only appear
        # once *.Id columns do (see "new" below).
        header = (
            "Power Volt   Curr   Tempr  Tlow   Thigh  Vlow   Vhigh  "
            "Base.St  Volt.St  Curr.St  Temp.St  Coulomb  "
            "Time                 B.V.St   B.T.St  "
        )

    rows: list[str] = []

    for g in range(1, n_groups + 1):
        for slot in range(1, slots_per_group + 1):
            bat_id = (g - 1) * slots_per_group + slot
            if bat_id_filter is not None and bat_id != bat_id_filter:
                continue
            present = slot <= batt_per_group and _faults.get(bat_id) != "absent"

            if present:
                # Deterministic-but-varied cell extremes per battery
                rng = random.Random(bat_id)
                tlow_id = rng.randint(0, cells - 1)
                thigh_id = rng.randint(0, cells - 1)
                vlow_id = rng.randint(0, cells - 1)
                vhigh_id = rng.randint(0, cells - 1)
                fault = _faults.get(bat_id)
                volt_st = (
                    "OV" if fault == "ov" else "UV" if fault == "uv" else "Normal  "
                )
                curr_st = "OC" if fault == "oc" else "Normal  "
                temp_st = (
                    "OT" if fault == "ot" else "UT" if fault == "ut" else "Normal  "
                )
                bv_st = volt_st
                bt_st = temp_st
                sys_alarm = "Alarm   " if fault and fault != "absent" else "Normal  "
                # Raw value overrides — telemetry-driven monitors fire alongside
                # status-string ones
                dv = s["voltage"]
                dc = s["current"]
                dt = s["temperature"]
                dvl = s["volt_low"]
                dvh = s["volt_high"]
                dtl = s["temp_low"]
                dth = s["temp_high"]
                if fault == "ov":
                    dv = 3870 * cells
                    dvl = 3865
                    dvh = 3870
                elif fault == "uv":
                    dv = 2750 * cells
                    dvl = 2750
                    dvh = 2760
                elif fault == "ot":
                    dt = 55000
                    dth = 55000
                elif fault == "ut":
                    dt = -10000
                    dtl = -10000
                elif fault == "oc":
                    dc = (
                        -99999 if s["current"] < 0 else 99999
                    )  # −99999 is 6 chars, fits 7-char column

                if firmware == "new":
                    mostempr = s["mostempr"] + rng.randint(-500, 500)
                    rows.append(
                        f"{bat_id:<6}{dv:<7}{dc:<7}{dt:<7}"
                        f"{dtl:<7}{tlow_id:<8}{dth:<7}{thigh_id:<9}"
                        f"{dvl:<7}{vlow_id:<9}{dvh:<7}{vhigh_id:<9}"
                        f"{base_st:<9}{volt_st:<9}{curr_st:<9}{temp_st:<9}"
                        f"{str(s['soc']) + '%':<9}{now}  "
                        f"{bv_st:<9}{bt_st:<9}{mostempr:<10}Normal   {sys_alarm}"
                    )
                else:
                    # B.T.St is the last column on "old" — 8-wide (2 trailing
                    # spaces), not the 9-wide mid-row field width, to match
                    # docs/docs.md's real capture byte-for-byte (it was
                    # previously followed by MosTempr, which swallowed the
                    # 9th padding column; now nothing follows it).
                    rows.append(
                        f"{bat_id:<6}{dv:<7}{dc:<7}{dt:<7}"
                        f"{dtl:<7}{dth:<7}{dvl:<7}{dvh:<7}"
                        f"{base_st:<9}{volt_st:<9}{curr_st:<9}{temp_st:<9}"
                        f"{str(s['soc']) + '%':<9}{now}  "
                        f"{bv_st:<9}{bt_st:<8}"
                    )
            else:
                if firmware == "new":
                    rows.append(
                        f"{bat_id:<6}-      -      -      -      -       -      -      "
                        "  -      -        -      -        Absent   -        -        -"
                        "        -        -                    -        -        -     "
                        "    -        -       "
                    )
                else:
                    rows.append(
                        f"{bat_id:<6}-      -      -      -      -      -      -      A"
                        "bsent   -        -        -        -        -                 "
                        "   -        -       "
                    )

    body = header + "\r\r\n" + "\r\r\n".join(rows)
    return _wrap(cmd, body)


# info
def _resp_info(cmd: str) -> bytes:
    m = MODELS[_cfg["model"]]
    body = (
        f"Device address      : 1\r\n\r"
        f"Manufacturer        : Pylon\r\n\r"
        f"Device name         : {m['device_name']}\r\n\r"
        f"Board version       : PHANTOMSAV10R03\r\n\r"
        f"Main Soft version   : {_cfg['fw_version']}\r\n\r"
        f"Soft  version       : V2.4\r\n\r"
        f"Boot  version       : V2.0\r\n\r"
        f"Comm version        : V2.0\r\n\r"
        f"Release Date        : 20-05-28\r\n\r"
        f"Barcode             : PPTBH02400710243\r\n\r"
        f"\r\n\r"
        f"Specification       : {m['spec']}\r\n\r"
        f"Cell Number         : {m['cells']}\r\n\r"
        f"Max Dischg Curr     : {m['max_dsg']}mA\r\n\r"
        f"Max Charge Curr     : {m['max_chg']}mA\r\n\r"
        f"EPONPort rate       : 1200\r\n\r"
        f"Console Port rate   : 115200"
    )
    return _wrap(cmd, body, kv=True)


# stat
def _resp_stat(cmd: str) -> bytes:
    with _state_lock:
        s = _state.copy()
    base_soh = _soh_pct(s["cycles"])
    body = (
        f"Device address           1\r\r\n"
        f"Data Items      :     1689\r\r\n"
        f"HisData Items   :     1794\r\r\n"
        f"MiscData Items  :     6230\r\r\n"
        f"Charge Cnt.     :  {s['charge_cnt']:>7}\r\r\n"
        f"Discharge Cnt.  :  {s['discharge_cnt']:>7}\r\r\n"
        f"Charge Times    :  {s['charge_times']:>7}\r\r\n"
        f"Status Cnt.     :     4680\r\r\n"
        f"Idle Times      :  {s['idle_times']:>7}\r\r\n"
        f"COC Times       :        0\r\r\n"
        f"DOC Times       :        0\r\r\n"
        f"COCA Times      :        0\r\r\n"
        f"DOCA Times      :        0\r\r\n"
        f"SC Times        :  {s['sc_times']:>7}\r\r\n"
        f"Bat OV Times    :  {s['bat_ov_times']:>7}\r\r\n"
        f"Bat HV Times    :  {s['bat_hv_times']:>7}\r\r\n"
        f"Bat LV Times    :  {s['bat_lv_times']:>7}\r\r\n"
        f"Bat UV Times    :  {s['bat_uv_times']:>7}\r\r\n"
        f"Bat SLP Times   :        0\r\r\n"
        f"Pwr OV Times    :  {s['pwr_ov_times']:>7}\r\r\n"
        f"Pwr HV Times    :  {s['pwr_hv_times']:>7}\r\r\n"
        f"Pwr LV Times    :        0\r\r\n"
        f"Pwr UV Times    :        0\r\r\n"
        f"Pwr SLP Times   :        0\r\r\n"
        f"COT Times       :        0\r\r\n"
        f"CUT Times       :        0\r\r\n"
        f"DOT Times       :        0\r\r\n"
        f"DUT Times       :        0\r\r\n"
        f"CHT Times       :        0\r\r\n"
        f"CLT Times       :        0\r\r\n"
        f"DHT Times       :        0\r\r\n"
        f"DLT Times       :        0\r\r\n"
        f"Shut Times      :  {s['shut_times']:>7}\r\r\n"
        f"Reset Times     :  {s['reset_times']:>7}\r\r\n"
        f"RV Times        :        0\r\r\n"
        f"Input OV Times  :        0\r\r\n"
        f"SOH Times       :        0\r\r\n"
        f"BMICERR Times   :        0\r\r\n"
        f"CYCLE Times     :  {s['cycles']:>7}\r\r\n"
        f"Sys SOH         :  {base_soh}%\r\r\n"
        f"Pwr Percent     :  {s['soc']:>7}\r\r\n"
        f"Pwr Coulomb     : {s['pwr_coulomb']}\r\r\n"
        f"Dsg Cap         : {s['dsg_cap']}\r\r\n"
        f"HT@0.5C Cnt     :        0\r\r\n"
        f"LT@0.5C Cnt     :        0\r\r\n"
        f"HT Cnt          :        0\r\r\n"
        f"LT Cnt          :        0\r\r\n"
        f"LV Cnt          :       76\r\r\n"
        f"LifeWarn Times  :  {s['life_warn_times']:>7}\r\r\n"
        f"LifeAlarm Times :  {s['life_alarm_times']:>7}"
    )
    return _wrap(cmd, body)


# time
def _resp_time(cmd: str) -> bytes:
    parts = cmd.split()
    if len(parts) > 1:
        # time YY MM DD HH MM SS  — set command, validate loosely and ack
        if len(parts) != 7:
            return _wrap(cmd, "Error: time YY MM DD HH MM SS", kv=True)
        return _wrap(cmd, "", kv=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _wrap(cmd, f"Ds3231 {now}", kv=True)


# bat [N]  — per-cell data with realistic variation
def _resp_bat(cmd: str) -> bytes:
    with _state_lock:
        s = _state.copy()
    m = MODELS[_cfg["model"]]
    cells = m["cells"]
    # Fades with cycle count, same as pwr N's "Real Coulomb" — previously a
    # flat model constant that never reflected the pack's simulated aging.
    cap = int(m["cap_mah"] * _soh_pct(s["cycles"]) / 100) // cells

    n_groups = _cfg["groups"]
    slots_per_group = _cfg["slots"]

    bat_id = _parse_bat_id_arg(cmd)

    if bat_id < 1 or bat_id > n_groups * slots_per_group:
        return _wrap(cmd, f"Battery {bat_id} not found")

    present, fault = _battery_slot_status(bat_id)

    if not present or fault == "absent":
        return _wrap(cmd, f"Battery {bat_id}\r\r\nAbsent")

    st = _base_state(s["current"])

    cell_volt_st = "OV" if fault == "ov" else "UV" if fault == "uv" else "Normal"
    cell_curr_st = "OC" if fault == "oc" else "Normal"
    cell_temp_st = "OT" if fault == "ot" else "UT" if fault == "ut" else "Normal"
    pack_curr = (
        (-150000 if s["current"] < 0 else 150000) if fault == "oc" else s["current"]
    )

    # docs/docs.md's real US2KBPL capture has no "SOC" header token at all —
    # each data row still carries the percentage immediately before Coulomb,
    # which is exactly the layout src/parser_schema.py's
    # _bat_header_postprocess exists to handle. Reproduce that on "old" so
    # the fallback path is exercised against a live stub, not just a
    # hand-written string in tests; "new" keeps the explicit column since
    # there's no real capture proving whether newer firmware adds it.
    if _cfg["firmware"] == "old":
        header = (
            "Battery  Volt     Curr     Tempr    "
            "Base State   Volt. State  Curr. State  Temp. State  Coulomb     "
        )
    else:
        header = (
            "Battery  Volt     Curr     Tempr    "
            "Base State   Volt. State  Curr. State  Temp. State  SOC        "
            "Coulomb     "
        )
    rows: list[str] = []
    rng = random.Random(bat_id)
    for c in range(cells):
        if fault == "ov":
            v = 3870 + rng.randint(0, 5)
        elif fault == "uv":
            v = 2750 + rng.randint(-5, 5)
        else:
            v = s["volt_low"] + rng.randint(0, max(0, s["volt_high"] - s["volt_low"]))
        if fault == "ot":
            t = 55000 + rng.randint(-500, 500)
        elif fault == "ut":
            t = -10000 + rng.randint(-500, 500)
        else:
            t = s["temperature"] + rng.randint(-800, 800)
        rows.append(
            f"{c:<9}{v:<9}{pack_curr:<9}{t:<9}"
            f"{st:<13}{cell_volt_st:<13}{cell_curr_st:<13}{cell_temp_st:<13}"
            f"{str(s['soc']) + '%':<11}{cap} mAH"
        )
    body = header + "\r\r\n" + "\r\r\n".join(rows)
    return _wrap(cmd, body)


# soh [N]  — per-cell SOH with realistic aging
def _resp_soh(cmd: str) -> bytes:
    with _state_lock:
        volt = _state["volt_low"]
        cycles = _state["cycles"]
    m = MODELS[_cfg["model"]]
    cells = m["cells"]

    n_groups = _cfg["groups"]
    slots_per_group = _cfg["slots"]

    bat_id = _parse_bat_id_arg(cmd)

    if bat_id < 1 or bat_id > n_groups * slots_per_group:
        return _wrap(cmd, f"Battery {bat_id} not found")

    present, fault = _battery_slot_status(bat_id)

    if not present or fault == "absent":
        return _wrap(cmd, f"Power   {bat_id}\r\r\nAbsent")

    # SOH degrades slightly with cycle count; each cell varies a little
    base_soh = _soh_pct(cycles)

    header = f"Power   {bat_id}\r\r\nBattery    Voltage    SOHCount   SOHStatus "
    rows = [header]
    rng = random.Random(bat_id)
    for c in range(cells):
        # docs/docs.md's real capture shows ±1-2 mV of per-cell spread here
        # (e.g. 3377-3379), not one repeated value across every row.
        cell_volt = volt + rng.randint(-2, 2)
        soh_count = max(0, base_soh - rng.randint(0, 3))
        rows.append(f"{c:<11}{cell_volt:<11}{soh_count:<11}Normal    ")
    return _wrap(cmd, "\r\r\n".join(rows))


# help
# Command set confirmed from real hardware dumps.  config / ctrl / prot /
# pwrsys / re are NOT in any verified help listing; they are kept functional
# as possible undocumented / admin-mode commands but omitted from help so
# that a client under test sees the same Unknown-command response for them
# that it would see on real hardware.
#
# LC.St (load-control state) appears between Coulomb and Time in some
# pre-.Id dumps but not others; the exact firmware boundary is unverified,
# so the "old" layout here omits it as an approximation.
_HELP_TEXT = (
    "Local command:\r\n\r"
    "bat      Battery data show - bat [pwr][index]\r\n\r"
    "cmdquit  Quit console mode - cmdquit\r\n\r"
    "data     History data load - data [event/history/misc][item]\r\n\r"
    "datalist Show recorded data - datalist [event/history/misc][item/bat]"
    "[batnun]\r\n\r"
    "disp     Display Info at regular intervals - disp [(pwrs pwrNo)/val]\r\n\r"
    "getpwr   Get power Info - getpwr\r\n\r"
    "help     Help [cmd]\r\n\r"
    "info     Device infomation - info\r\n\r"
    "log      Log information show - log\r\n\r"
    "login    Login Admin mode - login [password]\r\n\r"
    "logout   user mode - logout\r\n\r"
    "pwr      Power data show - pwr [index]\r\n\r"
    "shut     Shut down - shut\r\n\r"
    "soh      State of health - soh [addr]\r\n\r"
    "stat     Statistic data show - stat\r\n\r"
    "time     Time - time [year] [month] [day] [hour] [minute] [second]\r\n\r"
    "trst     Test Soft Reset - trst\r\n\r"
    "updata   updata system - updata\r\n\r"
    "**********************************************************\r\n\r"
    "Remote command:\r\n\r"
    "data     History data load\r\n\r"
    "info     Device infomation\r\n\r"
    "login    Login Admin mode\r\n\r"
    "logout   user mode\r\n\r"
    "soh      State of health\r\n\r"
    "stat     Statistic data show\r\n\r"
    "Press [Enter] to be continued,other key to exit"
)


def _resp_help(cmd: str) -> bytes:
    return _wrap(cmd, _HELP_TEXT)


# login / logout
def _resp_login(cmd: str) -> bytes:
    global _admin_mode
    parts = cmd.split()
    pw = parts[1] if len(parts) > 1 else ""
    if pw in ("", "000000", "000"):
        _admin_mode = True
        return _wrap(cmd, "Enter admin mode successfully", kv=True)
    return _wrap(cmd, "Password error", kv=True)


def _resp_logout(cmd: str) -> bytes:
    global _admin_mode
    _admin_mode = False
    return _wrap(cmd, "Logout successfully", kv=True)


# log
def _resp_log(cmd: str) -> bytes:
    with _state_lock:
        soc = _state["soc"]
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        f"Log count  : 5\r\r\n"
        f"1  {now}  Charge start  SOC={soc}%\r\r\n"
        f"2  {now}  Normal\r\r\n"
        f"3  {now}  Normal\r\r\n"
        f"4  {now}  Normal\r\r\n"
        f"5  {now}  Normal"
    )
    return _wrap(cmd, body)


# data / datalist  — abbreviated history stubs
def _resp_data(cmd: str) -> bytes:
    body = "Data Items : 0\r\r\nNo history data available"
    return _wrap(cmd, body)


def _resp_datalist(cmd: str) -> bytes:
    body = "DataList Items : 0\r\r\nNo history data available"
    return _wrap(cmd, body)


# disp  — single pwr snapshot (streaming not emulated)
def _resp_disp(cmd: str) -> bytes:
    return _resp_pwr("pwr")


# prot  — protection flags
def _resp_prot(cmd: str) -> bytes:
    # Reflect injected faults like every other view does — this was
    # previously the one command 'stub fault' didn't reach, contradicting
    # the module docstring's "affects all views" claim for admin control.
    active = set(_faults.values()) - {"absent"}
    volt_st = "Alarm" if active & {"ov", "uv"} else "Normal"
    curr_st = "Alarm" if "oc" in active else "Normal"
    temp_st = "Alarm" if active & {"ot", "ut"} else "Normal"
    sys_st = "Alarm" if active else "Normal"
    body = (
        "Protection flags:\r\r\n"
        f"Volt.Prot    : {volt_st}\r\r\n"
        f"Curr.Prot    : {curr_st}\r\r\n"
        f"Temp.Prot    : {temp_st}\r\r\n"
        f"SysAlarm     : {sys_st}"
    )
    return _wrap(cmd, body)


# pwrsys  — system power summary (LV-HUB)
def _resp_pwrsys(cmd: str) -> bytes:
    with _state_lock:
        s = _state.copy()
    n_groups = _cfg["groups"]
    batt_per_group = _cfg["batteries"]
    slots_per_group = _cfg["slots"]
    m = MODELS[_cfg["model"]]
    total = n_groups * batt_per_group
    cells = m["cells"]

    absent_count = sum(
        1
        for g in range(1, n_groups + 1)
        for slot in range(1, batt_per_group + 1)
        if _faults.get((g - 1) * slots_per_group + slot) == "absent"
    )
    online = total - absent_count
    base_soh = _soh_pct(s["cycles"])
    # FCC reflects both SOH degradation and currently-online module count
    fcc_mah = int(m["cap_mah"] * base_soh / 100) * online
    rc_mah = int(fcc_mah * s["soc"] / 100)
    sys_curr = s["current"] * n_groups
    chg_factor = 0.3 if s["soc"] > 95 else 1.0  # Taper charge near full
    dsg_factor = 0.3 if s["soc"] < 10 else 1.0  # Taper discharge near empty
    rec_chg_curr = int(m["max_chg"] * chg_factor) * n_groups
    rec_dsg_curr = int(abs(m["max_dsg"]) * dsg_factor) * n_groups
    rec_chg_v = 3650 * cells
    rec_dsg_v = 2800 * cells

    sys_alarm_st = "Alarm" if any(f != "absent" for f in _faults.values()) else "Normal"
    lines = [
        f"Groups       : {n_groups}",
        f"Modules/Group: {batt_per_group}",
        f"Total modules: {total}",
        f"Online       : {online}",
        f"Offline      : {absent_count}",
        f"Sys Voltage  : {s['voltage']}",
        f"Sys Current  : {sys_curr}",
        f"Sys SOC      : {s['soc']}%",
        f"Sys SOH      : {base_soh}%",
        f"Sys RC       : {rc_mah} mAH",
        f"Sys FCC      : {fcc_mah} mAH",
        f"Sys State    : {_base_state(s['current'])}",
        f"Sys Alarm    : {sys_alarm_st}",
        f"Sys Vlow     : {s['volt_low']} mV",
        f"Sys Vhigh    : {s['volt_high']} mV",
        f"Sys Tlow     : {s['temp_low']} mC",
        f"Sys Thigh    : {s['temp_high']} mC",
        f"Rec ChgVolt  : {rec_chg_v} mV",
        f"Rec DsgVolt  : {rec_dsg_v} mV",
        f"Rec ChgCurr  : {rec_chg_curr} mA",
        f"Rec DsgCurr  : {rec_dsg_curr} mA",
    ]
    if n_groups > 1:
        lines.append("")
        # Per-group currents that sum exactly to Sys Current
        group_currents: list[int] = []
        remaining = sys_curr
        for g in range(1, n_groups + 1):
            if g < n_groups:
                grng = random.Random(g)
                gc = s["current"] + grng.randint(-50, 50)
                group_currents.append(gc)
                remaining -= gc
            else:
                group_currents.append(remaining)
        for g in range(1, n_groups + 1):
            grng = random.Random(g)
            gv = s["voltage"] + grng.randint(-200, 200)
            gc = group_currents[g - 1]
            g_absent = sum(
                1
                for slot in range(1, batt_per_group + 1)
                if _faults.get((g - 1) * slots_per_group + slot) == "absent"
            )
            g_online = batt_per_group - g_absent
            lines.append(
                f"Group {g:<3}     Volt: {gv}  Curr: {gc}"
                f"  Online: {g_online}  SOC: {s['soc']}%  State: {_base_state(gc)}"
            )
    body = "\r\r\n".join(lines)
    return _wrap(cmd, body)


# stub  — runtime state injection (admin mode only)
def _resp_stub(cmd: str) -> bytes:
    """Admin-mode command for runtime state injection.

    stub fault <bat> <ov|uv|ot|ut|oc|absent>  — inject fault on battery <bat>
    stub clear <bat>                           — clear injected fault
    stub soc <pct>                             — override global SOC (0-100)
    stub current <mA>                          — fix current (persists until 'auto')
    stub current auto                          — return current to updater control
    """
    if not _admin_mode:
        return _wrap(cmd, "Admin mode required — login 000000", kv=True)
    parts = cmd.split()
    if len(parts) < 2:
        return _wrap(
            cmd,
            "Usage: stub fault <bat> <ov|uv|ot|ut|oc|absent> | stub clear <bat> | "
            "stub soc <pct> | stub current <mA|auto>",
            kv=True,
        )
    sub = parts[1].lower()
    if sub == "fault" and len(parts) >= 4:
        try:
            bat_id = int(parts[2])
        except ValueError:
            return _wrap(cmd, "Error: <bat> must be an integer", kv=True)
        fault_type = parts[3].lower()
        valid = {"ov", "uv", "ot", "ut", "oc", "absent"}
        if fault_type not in valid:
            return _wrap(
                cmd, f"Error: fault type must be one of {sorted(valid)}", kv=True
            )
        _faults[bat_id] = fault_type
        return _wrap(cmd, f"Fault '{fault_type}' injected on battery {bat_id}", kv=True)
    if sub == "clear" and len(parts) >= 3:
        try:
            bat_id = int(parts[2])
        except ValueError:
            return _wrap(cmd, "Error: <bat> must be an integer", kv=True)
        removed = _faults.pop(bat_id, None)
        msg = (
            f"Fault cleared on battery {bat_id}"
            if removed
            else f"No fault active on battery {bat_id}"
        )
        return _wrap(cmd, msg, kv=True)
    if sub == "soc" and len(parts) >= 3:
        try:
            pct = max(0, min(100, int(parts[2])))
        except ValueError:
            return _wrap(cmd, "Error: <pct> must be an integer 0-100", kv=True)
        with _state_lock:
            _state["soc"] = pct
        return _wrap(cmd, f"SOC set to {pct}%", kv=True)
    if sub == "current" and len(parts) >= 3:
        if parts[2].lower() == "auto":
            with _state_lock:
                _state["current_override"] = None
            return _wrap(cmd, "Current override cleared (auto mode resumed)", kv=True)
        try:
            ma = int(parts[2])
        except ValueError:
            return _wrap(cmd, "Error: <mA> must be an integer or 'auto'", kv=True)
        with _state_lock:
            _state["current"] = ma
            _state["charging"] = ma > 0
            _state["current_override"] = ma
        return _wrap(
            cmd,
            f"Current fixed at {ma} mA (use 'stub current auto' to resume)",
            kv=True,
        )
    return _wrap(
        cmd,
        "Usage: stub fault <bat> <ov|uv|ot|ut|oc|absent> | stub clear <bat> | "
        "stub soc <pct> | stub current <mA|auto>",
        kv=True,
    )


# cmdquit  — close the console session
class _ClientQuit(Exception):
    """Raised by _resp_cmdquit; caught in _BmsHandler.handle to close session."""


def _resp_cmdquit(cmd: str) -> bytes:  # return type is nominal; always raises
    raise _ClientQuit()


# shut / trst / updata  — stubs
def _resp_shut(cmd: str) -> bytes:
    return _wrap(cmd, "System will shut down", kv=True)


def _resp_trst(cmd: str) -> bytes:
    return _wrap(cmd, "Test reset complete", kv=True)


def _resp_updata(cmd: str) -> bytes:
    return _wrap(cmd, "No update available", kv=True)


# re <addr> <cmd>  — remote command forwarding
def _resp_re(cmd: str) -> bytes:
    """Forward a command to a remote battery address."""
    parts = cmd.split(None, 2)
    if len(parts) < 3:
        return _wrap(cmd, "Usage: re <addr> <command>", kv=True)
    remote_cmd = parts[2]
    # Dispatch the forwarded command exactly as if it were sent directly
    fwd_response = _dispatch(remote_cmd)
    # Wrap in a remote-forward envelope (simplified: just return the forwarded output)
    return fwd_response


# Dispatch table
def _dispatch(raw_line: str) -> bytes:
    cmd = raw_line.strip()
    tokens = cmd.split()
    base = tokens[0].lower() if tokens else ""

    if base in ("pwr", "getpwr"):
        return _resp_pwr(cmd)
    if base == "info":
        return _resp_info(cmd)
    if base == "stat":
        return _resp_stat(cmd)
    if base == "time":
        return _resp_time(cmd)
    if base == "bat":
        return _resp_bat(cmd)
    if base == "soh":
        return _resp_soh(cmd)
    if base == "help":
        return _resp_help(cmd)
    if base == "login":
        return _resp_login(cmd)
    if base == "logout":
        return _resp_logout(cmd)
    if base == "log":
        return _resp_log(cmd)
    if base == "data":
        return _resp_data(cmd)
    if base == "datalist":
        return _resp_datalist(cmd)
    if base == "disp":
        return _resp_disp(cmd)
    if base == "cmdquit":
        _resp_cmdquit(cmd)  # always raises _ClientQuit
    if base == "shut":
        return _resp_shut(cmd)
    if base == "trst":
        return _resp_trst(cmd)
    if base == "updata":
        return _resp_updata(cmd)
    # Undocumented / admin-mode commands — functional but absent from help.
    if base == "prot":
        return _resp_prot(cmd)
    if base == "pwrsys":
        return _resp_pwrsys(cmd)
    if base == "re":
        return _resp_re(cmd)
    if base == "stub":
        return _resp_stub(cmd)
    if base == "":
        return _PROMPT

    return _unknown(cmd)


# TCP server
class _BmsHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        addr = f"{self.client_address[0]}:{self.client_address[1]}"
        print(f"[stub] + connected   {addr}")
        try:
            self.wfile.write(_PROMPT)
            self.wfile.flush()
            for raw_line in self.rfile:
                try:
                    response = _dispatch(raw_line.decode("ascii", errors="ignore"))
                except _ClientQuit:
                    self.wfile.write(b"Quit console mode\r\n\r$$\r\n\rpylon>")
                    self.wfile.flush()
                    break
                self.wfile.write(response)
                self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            print(f"[stub] - disconnected {addr}")


class _StubServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# Entry point
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pylontech BMS RS232 TCP Stub Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)"
    )
    ap.add_argument(
        "--port",
        default=12300,
        type=int,
        help="TCP port; 0 picks a free one — read it from the "
        '"[stub] listening on" line (default: 12300)',
    )
    ap.add_argument(
        "--batteries",
        default=2,
        type=int,
        metavar="N",
        help="Present modules per group, 1-16 (default: 2)",
    )
    ap.add_argument(
        "--slots",
        default=8,
        type=int,
        metavar="N",
        help="Total pwr rows per group, 1-16 (default: 8)",
    )
    ap.add_argument(
        "--groups",
        default=1,
        type=int,
        metavar="N",
        help="Number of parallel groups / LV-HUB groups, 1-6 (default: 1)",
    )
    ap.add_argument(
        "--model",
        default="US2000",
        choices=list(MODELS),
        help="Battery model: US2000 | US3000 | US5000 (default: US2000)",
    )
    ap.add_argument(
        "--firmware",
        default="new",
        choices=["old", "new"],
        help="Column layout: old (16 cols) | new (23 cols with *.Id, default: new)",
    )
    ap.add_argument(
        "--fw-version",
        default="B66.6",
        metavar="VERSION",
        help=(
            "'info' response's Main Soft version field, e.g. B66.6 or B84.5 "
            "(default: B66.6). Independent of --firmware: that flag picks the "
            "pwr table's column layout, this sets the version string a client "
            "reads back over the wire."
        ),
    )
    ap.add_argument(
        "--soc",
        default=85,
        type=int,
        metavar="PCT",
        help="Starting SOC %% (default: 85)",
    )
    ap.add_argument(
        "--tick-interval",
        default=30,
        type=int,
        metavar="SECONDS",
        help=(
            "Seconds between simulated state ticks (SOC/current/temperature "
            "drift). Tests asserting exact startup values should raise this "
            "well past the test run's wall-clock length so no tick fires "
            "mid-run (default: 30)"
        ),
    )
    args = ap.parse_args()

    if args.batteries < 1 or args.batteries > 16:
        ap.error("--batteries must be 1-16")
    if args.slots < 1 or args.slots > 16:
        ap.error("--slots must be 1-16")
    if args.slots < args.batteries:
        ap.error(
            f"--slots ({args.slots}) cannot be less than --batteries ({args.batteries})"
        )
    if args.groups < 1 or args.groups > 6:
        ap.error("--groups must be 1-6")
    if args.tick_interval < 1:
        ap.error("--tick-interval must be >= 1")
    if not args.fw_version.strip():
        ap.error("--fw-version must not be empty")

    _cfg["model"] = args.model
    _cfg["batteries"] = args.batteries
    _cfg["slots"] = args.slots
    _cfg["groups"] = args.groups
    _cfg["firmware"] = args.firmware
    _cfg["fw_version"] = args.fw_version
    _cfg["tick_interval"] = args.tick_interval
    _state["soc"] = args.soc

    threading.Thread(target=_state_updater, daemon=True).start()

    m = MODELS[args.model]
    total_present = args.batteries * args.groups
    total_slots = args.slots * args.groups
    # Bind before printing so --port 0 (OS-assigned) reports the real port.
    with _StubServer((args.host, args.port), _BmsHandler) as server:
        port = server.server_address[1]
        print("[stub] Pylontech BMS stub")
        print(f"[stub]   address  : {args.host}:{port}")
        print(f"[stub]   model    : {args.model}  ({m['device_name']}, {m['spec']})")
        print(f"[stub]   groups   : {args.groups}")
        print(
            f"[stub]   batteries: {total_present} present / {total_slots} slots total"
        )
        print(f"[stub]   firmware : {args.firmware}  (fw version {args.fw_version})")
        print(f"[stub]   SOC start: {args.soc}%")
        print(f"[stub]   HA config: TCP Socket  {args.host}:{port}")
        print()
        # Machine-readable startup handshake: tests/conftest.py's start_stub()
        # blocks on this exact line to learn the bound port instead of racing
        # a connect loop against a fixed port number. Keep the format in sync
        # with its _STUB_READY_RE. flush=True pushes the whole banner through
        # the pipe even when stdout is block-buffered (a subprocess pipe).
        print(f"[stub] listening on {args.host}:{port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
