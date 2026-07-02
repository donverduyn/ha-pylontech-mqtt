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

Firmware variants
-----------------
  old   18 columns — Volt … M.T.St  (pre-Tlow.Id era)
  new   23 columns — adds *.Id columns and SysAlarm.St  (default)
"""

import argparse
import datetime
import random
import socketserver
import threading
import time

# ---------------------------------------------------------------------------
# Model catalogue
# ---------------------------------------------------------------------------
MODELS: dict[str, dict] = {
    "US2000": dict(
        device_name="US2KBPL",
        spec="48V/50AH",
        cells=15,
        cap_mah=50000,
        max_chg=102000,
        max_dsg=-100000,
    ),
    "US3000": dict(
        device_name="US3KBPL",
        spec="48V/74AH",
        cells=15,
        cap_mah=74000,
        max_chg=150000,
        max_dsg=-150000,
    ),
    "US5000": dict(
        device_name="US5KBPL",
        spec="48V/100AH",
        cells=15,
        cap_mah=100000,
        max_chg=200000,
        max_dsg=-200000,
    ),
}

# ---------------------------------------------------------------------------
# Runtime configuration  (filled by main() before server starts)
# ---------------------------------------------------------------------------
_cfg: dict = {
    "model": "US2000",
    "batteries": 2,  # present modules per group
    "slots": 8,  # total pwr rows per group  (slots >= batteries)
    "groups": 1,  # number of parallel groups (LV-HUB)
    "firmware": "new",  # "old" (18-col) or "new" (23-col with *.Id)
}

# ---------------------------------------------------------------------------
# Shared BMS state
# ---------------------------------------------------------------------------
_state: dict = {
    "soc": 85,
    "charging": True,
    "voltage": 50691,  # mV  (stack voltage)
    "current": 3806,  # mA  (positive = charging)
    "temperature": 17000,  # mK  (pack temperature)
    "temp_low": 13000,  # mK  min cell temperature
    "temp_high": 14000,  # mK  max cell temperature
    "volt_low": 3378,  # mV  min cell voltage
    "volt_high": 3381,  # mV  max cell voltage
    "mostempr": 22700,  # mK  MOSFET temperature
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
}
_state_lock = threading.Lock()
_admin_mode = False  # toggled by login/logout


# ---------------------------------------------------------------------------
# Background state updater
# ---------------------------------------------------------------------------
def _state_updater() -> None:
    while True:
        time.sleep(30)
        with _state_lock:
            s = _state
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
            base_mv = 3000 + int(s["soc"] * 6.5)
            s["volt_low"] = base_mv + random.randint(0, 3)
            s["volt_high"] = s["volt_low"] + random.randint(1, 8)
            s["voltage"] = s["volt_low"] * 15
            # MOS temperature tracks pack temp with a small offset
            s["mostempr"] = s["temperature"] + random.randint(1000, 3000)


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------
_PROMPT = b"\r\npylon>"


def _wrap(cmd_echo: str, body: str, kv: bool = False) -> bytes:
    after = "\n\r" if kv else "\r\n"
    text = f"{cmd_echo}\n\r@\r{after}{body}\r\n\rCommand completed successfully\r\n\r$$\r\n\rpylon>"
    return text.encode("ascii", errors="replace")


def _unknown(cmd: str) -> bytes:
    base = cmd.split()[0] if cmd.split() else cmd
    return f"{cmd}\r\nUnknown command '{base}'\r\n\r$$\r\n\rpylon>".encode("ascii")


# ---------------------------------------------------------------------------
# pwr N (indexed) — vertical per-battery key:value block
# ---------------------------------------------------------------------------
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
        s = dict(_state)

    n_groups = _cfg["groups"]
    slots_per_group = _cfg["slots"]
    batt_per_group = _cfg["batteries"]
    m = MODELS[_cfg["model"]]

    if bat_id < 1 or bat_id > n_groups * slots_per_group:
        return _wrap(cmd, f"Power {bat_id} not found", kv=True)

    slot_in_group = ((bat_id - 1) % slots_per_group) + 1
    present = slot_in_group <= batt_per_group

    SEP = "----------------------------"

    def kv(key: str, value: object, unit: str = "") -> str:
        # Produces: '{key:<16}: {value:<8}{unit}'
        # With the leading \r from framing:
        #   line_str[1:18] == f'{key:<16}:'   ← key check
        #   line_str[19:27] == f'{value:<8}'  ← 8-char value field
        return f"{key:<16}: {str(value):<8}{unit}"

    if not present:
        lines = [SEP, f"Power {bat_id}", kv("Basic Status", "Absent"), SEP]
    else:
        base_st = _base_state(s["current"])
        lines = [
            SEP,
            f"Power {bat_id}",
            kv("Voltage", s["voltage"], "mV"),
            kv("SOC Voltage", "0", "mV"),
            kv("Current", s["current"], "mA"),
            kv("Temperature", s["temperature"], "mC"),
            kv("Coulomb", s["soc"], "%"),
            kv("Total Coulomb", m["cap_mah"], "mAH"),
            kv("Real Coulomb", m["cap_mah"], "mAH"),
            kv("Total Power In", s["pwr_coulomb"], "AS"),
            kv("Total Power Out", s["dsg_cap"], "AS"),
            kv("Basic Status", base_st),
            kv("Volt Status", "Normal"),
            kv("Current Status", "Normal"),
            kv("Tmpr. Status", "Normal"),
            kv("Coul. Status", "Normal"),
            kv("Bat Status", "VNOR TNOR"),
            kv("CMOS Status", "OFF"),
            kv("DMOS Status", "OFF"),
            # Protection enable masks — value exceeds 8 chars; formatted directly.
            f"{'Bat Protect ENA':<16}: OV HV LV UV SLP OT HT LT UT",
            f"{'Pwr Protect ENA':<16}: OV HV LV UV SLP OT HT LT UT COC COC2 COCA DOCA DOC DOC2 SC",
            kv("Bat Events", "0x0"),
            kv("Power Events", "0x0"),
            kv("System Fault", "0x0"),
            kv("COMM EX Status", "0x0"),
            SEP,
        ]

    body = "\r\n\r".join(lines)
    return _wrap(cmd, body, kv=True)


# ---------------------------------------------------------------------------
# pwr
# ---------------------------------------------------------------------------
def _base_state(current_ma: int) -> str:
    if abs(current_ma) < 500:
        return "Idle"
    return "Charge" if current_ma > 0 else "Discharge"


def _resp_pwr(cmd: str) -> bytes:
    with _state_lock:
        s = dict(_state)

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
        try:
            bat_id_filter = int(parts[1])
        except ValueError:
            pass

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
        header = (
            "Power Volt   Curr   Tempr  Tlow   Thigh  Vlow   Vhigh  "
            "Base.St  Volt.St  Curr.St  Temp.St  Coulomb  "
            "Time                 B.V.St   B.T.St   MosTempr  M.T.St  "
        )

    rows: list[str] = []

    for g in range(1, n_groups + 1):
        for slot in range(1, slots_per_group + 1):
            bat_id = (g - 1) * slots_per_group + slot
            if bat_id_filter is not None and bat_id != bat_id_filter:
                continue
            present = slot <= batt_per_group

            if present:
                # Deterministic-but-varied cell extremes per battery
                rng = random.Random(bat_id)
                tlow_id = rng.randint(0, cells - 1)
                thigh_id = rng.randint(0, cells - 1)
                vlow_id = rng.randint(0, cells - 1)
                vhigh_id = rng.randint(0, cells - 1)
                mostempr = s["mostempr"] + rng.randint(-500, 500)

                if firmware == "new":
                    rows.append(
                        f"{bat_id:<6}{s['voltage']:<7}{s['current']:<7}{s['temperature']:<7}"
                        f"{s['temp_low']:<7}{tlow_id:<8}{s['temp_high']:<7}{thigh_id:<9}"
                        f"{s['volt_low']:<7}{vlow_id:<9}{s['volt_high']:<7}{vhigh_id:<9}"
                        f"{base_st:<9}Normal   Normal   Normal   "
                        f"{s['soc']}%      {now}  "
                        f"Normal   Normal   {mostempr:<10}Normal   Normal  "
                    )
                else:
                    rows.append(
                        f"{bat_id:<6}{s['voltage']:<7}{s['current']:<7}{s['temperature']:<7}"
                        f"{s['temp_low']:<7}{s['temp_high']:<7}{s['volt_low']:<7}{s['volt_high']:<7}"
                        f"{base_st:<9}Normal   Normal   Normal   "
                        f"{s['soc']}%      {now}  "
                        f"Normal   Normal   {mostempr:<10}Normal  "
                    )
            else:
                if firmware == "new":
                    rows.append(
                        f"{bat_id:<6}-      -      -      -      -       -      -        "
                        "-      -        -      -        "
                        "Absent   -        -        -        -        -                    "
                        "-        -        -         -        -       "
                    )
                else:
                    rows.append(
                        f"{bat_id:<6}-      -      -      -      -      -      -      "
                        "Absent   -        -        -        -        -                    "
                        "-        -        -         -       "
                    )

    body = header + "\r\r\n" + "\r\r\n".join(rows)
    return _wrap(cmd, body)


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------
def _resp_info(cmd: str) -> bytes:
    m = MODELS[_cfg["model"]]
    body = (
        f"Device address      : 1\r\n\r"
        f"Manufacturer        : Pylon\r\n\r"
        f"Device name         : {m['device_name']}\r\n\r"
        f"Board version       : PYLONSTUBV10R01\r\n\r"
        f"Main Soft version   : B66.6\r\n\r"
        f"Soft  version       : V2.4\r\n\r"
        f"Boot  version       : V2.0\r\n\r"
        f"Comm version        : V2.0\r\n\r"
        f"Release Date        : 20-05-28\r\n\r"
        f"Barcode             : PYLONSTUB0000001\r\n\r"
        f"\r\n\r"
        f"Specification       : {m['spec']}\r\n\r"
        f"Cell Number         : {m['cells']}\r\n\r"
        f"Max Dischg Curr     : {m['max_dsg']}mA\r\n\r"
        f"Max Charge Curr     : {m['max_chg']}mA\r\n\r"
        f"EPONPort rate       : 1200\r\n\r"
        f"Console Port rate   : 115200"
    )
    return _wrap(cmd, body, kv=True)


# ---------------------------------------------------------------------------
# stat
# ---------------------------------------------------------------------------
def _resp_stat(cmd: str) -> bytes:
    with _state_lock:
        s = dict(_state)
    body = (
        f"Device address           1\r\r\n"
        f"Data Items      :     1689\r\r\n"
        f"HisData Items   :     1794\r\r\n"
        f"MiscData Items  :     6230\r\r\n"
        f"Charge Cnt.     :  {s['charge_times']:>7}\r\r\n"
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


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------
def _resp_time(cmd: str) -> bytes:
    parts = cmd.split()
    if len(parts) > 1:
        # time YY MM DD HH MM SS  — set command, validate loosely and ack
        if len(parts) != 7:
            return _wrap(cmd, "Error: time YY MM DD HH MM SS", kv=True)
        return _wrap(cmd, "", kv=True)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _wrap(cmd, f"Ds3231 {now}", kv=True)


# ---------------------------------------------------------------------------
# bat [N]  — per-cell data with realistic variation
# ---------------------------------------------------------------------------
def _resp_bat(cmd: str) -> bytes:
    with _state_lock:
        s = dict(_state)
    m = MODELS[_cfg["model"]]
    cells = m["cells"]
    cap = m["cap_mah"] // cells
    st = "Charge" if s["charging"] else "Dischg"
    pack_curr = s["current"]  # same pack current repeated for every cell row

    header = (
        "Battery  Volt     Curr     Tempr    "
        "Base State   Volt. State  Curr. State  Temp. State  Coulomb     "
    )
    rows: list[str] = []
    for c in range(cells):
        # Slight temperature and voltage variation per cell
        t = s["temperature"] + random.randint(-800, 800)
        v = s["volt_low"] + random.randint(
            0, max(1, s["volt_high"] - s["volt_low"] + 3)
        )
        rows.append(
            f"{c:<9}{v:<9}{pack_curr:<9}{t:<9}"
            f"{st:<13}Normal       Normal       Normal        {s['soc']}%      {cap} mAH"
        )
    body = header + "\r\r\n" + "\r\r\n".join(rows)
    return _wrap(cmd, body)


# ---------------------------------------------------------------------------
# soh [N]  — per-cell SOH with realistic aging
# ---------------------------------------------------------------------------
def _resp_soh(cmd: str) -> bytes:
    with _state_lock:
        volt = _state["volt_low"]
        cycles = _state["cycles"]
    m = MODELS[_cfg["model"]]
    cells = m["cells"]

    # SOH degrades slightly with cycle count; each cell varies a little
    base_soh = max(0, 100 - int(cycles * 0.02))

    header = "Power   1\r\r\nBattery    Voltage    SOHCount   SOHStatus "
    rows = [header]
    for c in range(cells):
        soh_count = max(0, base_soh - random.randint(0, 3))
        rows.append(f"{c:<11}{volt:<11}{soh_count:<11}Normal    ")
    return _wrap(cmd, "\r\r\n".join(rows))


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------
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
    "datalist Show recorded data - datalist [event/history/misc][item/bat][batnun]\r\n\r"
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


# ---------------------------------------------------------------------------
# login / logout
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------
def _resp_log(cmd: str) -> bytes:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        f"Log count  : 5\r\r\n"
        f"1  {now}  Charge start  SOC=85%\r\r\n"
        f"2  {now}  Normal\r\r\n"
        f"3  {now}  Normal\r\r\n"
        f"4  {now}  Normal\r\r\n"
        f"5  {now}  Normal"
    )
    return _wrap(cmd, body)


# ---------------------------------------------------------------------------
# data / datalist  — abbreviated history stubs
# ---------------------------------------------------------------------------
def _resp_data(cmd: str) -> bytes:
    body = "Data Items : 0\r\r\nNo history data available"
    return _wrap(cmd, body)


def _resp_datalist(cmd: str) -> bytes:
    body = "DataList Items : 0\r\r\nNo history data available"
    return _wrap(cmd, body)


# ---------------------------------------------------------------------------
# disp  — single pwr snapshot (streaming not emulated)
# ---------------------------------------------------------------------------
def _resp_disp(cmd: str) -> bytes:
    return _resp_pwr("pwr")


# ---------------------------------------------------------------------------
# prot  — protection flags
# ---------------------------------------------------------------------------
def _resp_prot(cmd: str) -> bytes:
    body = (
        "Protection flags:\r\r\n"
        "Volt.Prot    : Normal\r\r\n"
        "Curr.Prot    : Normal\r\r\n"
        "Temp.Prot    : Normal\r\r\n"
        "SysAlarm     : Normal"
    )
    return _wrap(cmd, body)


# ---------------------------------------------------------------------------
# pwrsys  — system power summary (LV-HUB)
# ---------------------------------------------------------------------------
def _resp_pwrsys(cmd: str) -> bytes:
    with _state_lock:
        s = dict(_state)
    n_groups = _cfg["groups"]
    batt_per_group = _cfg["batteries"]
    total = n_groups * batt_per_group
    body = (
        f"Groups       : {n_groups}\r\r\n"
        f"Modules/Group: {batt_per_group}\r\r\n"
        f"Total modules: {total}\r\r\n"
        f"Online       : {total}\r\r\n"
        f"Offline      : 0\r\r\n"
        f"Sys Voltage  : {s['voltage']}\r\r\n"
        f"Sys Current  : {s['current'] * n_groups}\r\r\n"
        f"Sys SOC      : {s['soc']}%\r\r\n"
        f"Sys State    : {_base_state(s['current'])}"
    )
    return _wrap(cmd, body)


# ---------------------------------------------------------------------------
# cmdquit  — close the console session
# ---------------------------------------------------------------------------
class _ClientQuit(Exception):
    """Raised by _resp_cmdquit; caught in _BmsHandler.handle to close session."""


def _resp_cmdquit(cmd: str) -> bytes:  # return type is nominal; always raises
    raise _ClientQuit()


# ---------------------------------------------------------------------------
# shut / trst / updata  — stubs
# ---------------------------------------------------------------------------
def _resp_shut(cmd: str) -> bytes:
    return _wrap(cmd, "System will shut down", kv=True)


def _resp_trst(cmd: str) -> bytes:
    return _wrap(cmd, "Test reset complete", kv=True)


def _resp_updata(cmd: str) -> bytes:
    return _wrap(cmd, "No update available", kv=True)


# ---------------------------------------------------------------------------
# re <addr> <cmd>  — remote command forwarding
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------
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
    if base == "":
        return _PROMPT

    return _unknown(cmd)


# ---------------------------------------------------------------------------
# TCP server
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pylontech BMS RS232 TCP Stub Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)"
    )
    ap.add_argument("--port", default=12300, type=int, help="TCP port (default: 12300)")
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
        help="Column layout: old (18 cols) | new (23 cols with *.Id, default: new)",
    )
    ap.add_argument(
        "--soc",
        default=85,
        type=int,
        metavar="PCT",
        help="Starting SOC %% (default: 85)",
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

    _cfg["model"] = args.model
    _cfg["batteries"] = args.batteries
    _cfg["slots"] = args.slots
    _cfg["groups"] = args.groups
    _cfg["firmware"] = args.firmware
    _state["soc"] = args.soc

    threading.Thread(target=_state_updater, daemon=True).start()

    m = MODELS[args.model]
    total_present = args.batteries * args.groups
    total_slots = args.slots * args.groups
    print("[stub] Pylontech BMS stub")
    print(f"[stub]   address  : {args.host}:{args.port}")
    print(f"[stub]   model    : {args.model}  ({m['device_name']}, {m['spec']})")
    print(f"[stub]   groups   : {args.groups}")
    print(f"[stub]   batteries: {total_present} present / {total_slots} slots total")
    print(f"[stub]   firmware : {args.firmware}")
    print(f"[stub]   SOC start: {args.soc}%")
    print(f"[stub]   HA config: TCP Socket  {args.host}:{args.port}")
    print()

    with _StubServer((args.host, args.port), _BmsHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
