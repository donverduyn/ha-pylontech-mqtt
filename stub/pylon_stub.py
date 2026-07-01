#!/usr/bin/env python3
"""
Pylontech BMS RS232  →  TCP Stub Server
========================================
Emulates the full Pylontech RS232 console protocol over a raw TCP socket so
that ha-pylon-integration can be developed and tested without physical hardware.

Configure the integration as:
  Connection type : TCP Socket
  Host            : 127.0.0.1   (or wherever this script runs)
  Port            : 12300        (default, see --port)
  Poll interval   : 15           (seconds)

Usage
-----
  python pylon_stub.py                                # 2× US2000, port 12300
  python pylon_stub.py --batteries 3 --model US5000  # 3× US5000
  python pylon_stub.py --port 9999 --host 0.0.0.0    # bind to all interfaces
  python pylon_stub.py --help

Protocol parity
---------------
The stub responds to every command documented in docs.md:
  pwr           – power table (all slots, present + Absent rows)
  info          – device information
  stat          – full statistics (18 counters, cycles, coulombs …)
  time          – read BMS clock  /  time YY MM DD HH MM SS  (set, ack only)
  bat [N]       – per-cell data for one power module
  soh [N]       – per-cell SOH data for one power module
  help          – command list
  getpwr        – alias for pwr (returns same table)

Anything else returns an "Unknown command" error in BMS style.

The stub slowly varies SOC and current in a background thread so the
integration sees realistic changing values.
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
    "US2000": dict(device_name="US2KBPL",  spec="48V/50AH",  cells=15, cap_mah= 50000, max_chg= 102000, max_dsg=-100000),
    "US3000": dict(device_name="US3KBPL",  spec="48V/74AH",  cells=15, cap_mah= 74000, max_chg= 150000, max_dsg=-150000),
    "US5000": dict(device_name="US5KBPL",  spec="48V/100AH", cells=15, cap_mah=100000, max_chg= 200000, max_dsg=-200000),
}

# ---------------------------------------------------------------------------
# Runtime configuration  (filled by main() before server starts)
# ---------------------------------------------------------------------------
_cfg: dict = {
    "model":     "US2000",
    "batteries": 2,          # present battery modules
    "slots":     8,          # total pwr table rows (slots ≥ batteries)
}

# ---------------------------------------------------------------------------
# Shared mutable BMS state – all access through _state_lock
# ---------------------------------------------------------------------------
_state: dict = {
    "soc":              85,        # %
    "charging":         True,      # True → Charge  /  False → Discharge
    "voltage":          50691,     # mV  (module voltage)
    "current":          3806,      # mA  (positive = charging)
    "temperature":      17000,     # mK
    "temp_low":         13000,     # mK  (min cell temp)
    "temp_high":        14000,     # mK  (max cell temp)
    "volt_low":         3378,      # mV  (min cell voltage)
    "volt_high":        3381,      # mV  (max cell voltage)
    "cycles":           430,
    "charge_times":     1150,
    "discharge_cnt":    0,
    "idle_times":       23858,
    "shut_times":       329,
    "reset_times":      67,
    "sc_times":         0,
    "bat_ov_times":     56,
    "bat_hv_times":     5832,
    "bat_lv_times":     0,
    "bat_uv_times":     0,
    "pwr_ov_times":     4688,
    "pwr_hv_times":     6734,
    "life_warn_times":  0,
    "life_alarm_times": 0,
    "pwr_coulomb":      153311400,
    "dsg_cap":          21506462,
}
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Background state updater  (simulates slow charge/discharge cycles)
# ---------------------------------------------------------------------------
def _state_updater() -> None:
    while True:
        time.sleep(30)
        with _state_lock:
            s = _state
            if s["charging"]:
                s["soc"]     = min(100, s["soc"] + 1)
                s["current"] = random.randint(3000, 5000)
                if s["soc"] >= 100:
                    s["charging"]      = False
                    s["current"]       = -random.randint(1000, 3000)
                    s["discharge_cnt"] += 1
            else:
                s["soc"]     = max(0, s["soc"] - 1)
                s["current"] = -random.randint(1000, 4000)
                if s["soc"] <= 10:
                    s["charging"]      = True
                    s["current"]       = random.randint(3000, 5000)
                    s["cycles"]        += 1
                    s["charge_times"]  += 1
            # Cell voltages track SOC roughly (3.0 V empty → 3.65 V full)
            base_mv         = 3000 + int(s["soc"] * 6.5)
            s["volt_low"]   = base_mv + random.randint(0, 2)
            s["volt_high"]  = s["volt_low"] + random.randint(1, 5)
            s["voltage"]    = s["volt_low"] * 15   # 15 S stack


# ---------------------------------------------------------------------------
# BMS response builders
# ---------------------------------------------------------------------------
# Terminal prompt sent at the end of every response
_PROMPT     = b"\r\npylon>"
# Bare newline (channel prime) → just re-prompt, no data
_JUST_PROMPT = b"\r\npylon>"


def _wrap(cmd_echo: str, body: str) -> bytes:
    """Wrap body in the standard BMS response envelope."""
    text = f"{cmd_echo}\n\r@\r\r\n{body}\r\nCommand completed successfully\r\n\r$$\r\n\rpylon>"
    return text.encode("ascii", errors="replace")


def _resp_pwr(cmd: str) -> bytes:
    with _state_lock:
        s = dict(_state)
    m    = MODELS[_cfg["model"]]
    n    = _cfg["batteries"]
    slots = _cfg["slots"]
    st   = "Charge" if s["charging"] else "Discharge"
    now  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header = (
        "Power Volt   Curr   Tempr  Tlow   Thigh  Vlow   Vhigh  "
        "Base.St  Volt.St  Curr.St  Temp.St  Coulomb  Time                 B.V.St   B.T.St  "
    )
    rows: list[str] = []
    for i in range(1, slots + 1):
        if i <= n:
            rows.append(
                f"{i:<6}{s['voltage']:<7}{s['current']:<7}{s['temperature']:<7}"
                f"{s['temp_low']:<7}{s['temp_high']:<7}{s['volt_low']:<7}{s['volt_high']:<7}"
                f"{st:<9}Normal   Normal   Normal   {s['soc']}%      {now}  Normal   Normal  "
            )
        else:
            rows.append(
                f"{i:<6}-      -      -      -      -      -      -      "
                "Absent   -        -        -        -        -                    -        -       "
            )
    body = header + "\r\r\n" + "\r\r\n".join(rows)
    return _wrap(cmd, body)


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
    return _wrap(cmd, body)


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


def _resp_time(cmd: str) -> bytes:
    parts = cmd.split()
    if len(parts) > 1:
        # time YY MM DD HH MM SS  → set command, acknowledge only
        return _wrap(cmd, "")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _wrap(cmd, f"Ds3231 {now}")


def _resp_bat(cmd: str) -> bytes:
    """bat [N]  — per-cell data for one power module (N defaults to 1)."""
    with _state_lock:
        s = dict(_state)
    m     = MODELS[_cfg["model"]]
    cells = m["cells"]
    cap   = m["cap_mah"] // cells
    st    = "Charge" if s["charging"] else "Discharge"
    curr_per_cell = abs(s["current"]) // cells

    header = (
        "Battery  Volt     Curr     Tempr    "
        "Base State   Volt. State  Curr. State  Temp. State  Coulomb     "
    )
    rows: list[str] = []
    for c in range(cells):
        v = s["volt_low"] + random.randint(0, s["volt_high"] - s["volt_low"])
        rows.append(
            f"{c:<9}{v:<9}{curr_per_cell:<9}{s['temperature']:<9}"
            f"{st:<13}Normal       Normal       Normal        {s['soc']}%      {cap} mAH"
        )
    body = header + "\r\r\n" + "\r\r\n".join(rows)
    return _wrap(cmd, body)


def _resp_soh(cmd: str) -> bytes:
    """soh [N]  — per-cell SOH for one power module."""
    with _state_lock:
        volt = _state["volt_low"]
    m     = MODELS[_cfg["model"]]
    cells = m["cells"]

    header = "Power   1\r\r\nBattery    Voltage    SOHCount   SOHStatus "
    rows   = [header]
    for c in range(cells):
        rows.append(f"{c:<11}{volt:<11}0          Normal    ")
    return _wrap(cmd, "\r\r\n".join(rows))


def _resp_help(cmd: str) -> bytes:
    body = (
        "Local command:\r\n\r"
        "bat      Battery data show - bat [pwr][index]\r\n\r"
        "data     History data load - data [event/history/misc][item]\r\n\r"
        "datalist Show recorded data - datalist [event/history/misc][item/bat][batnun][volt/curr/temp/coul][item]\r\n\r"
        "disp     Display Info at regular intervals - disp [(pwrs pwrNo)/val]/[(bats batNo)/volt/curr/temp]\r\n\r"
        "getpwr   Get power Info - getpwr\r\n\r"
        "help     Help [cmd]\r\n\r"
        "info     Device infomation - info\r\n\r"
        "log      Log information show - log\r\n\r"
        "login    Login Admin mode - login [password]\r\n\r"
        "logout   user mode  - logout\r\n\r"
        "pwr      Power data show - pwr [index]\r\n\r"
        "soh      State of health - soh [addr]\r\n\r"
        "stat     Statistic data show - stat\r\n\r"
        "time     Time - time [year] [month] [day] [hour] [minute] [second]\r\n\r"
        "trst     Test Soft Reset - trst\r\n\r"
        "**********************************************************"
    )
    return _wrap(cmd, body)


def _dispatch(raw_line: str) -> bytes:
    """Route one command line to the appropriate response builder."""
    cmd  = raw_line.strip()
    base = cmd.split()[0].lower() if cmd.split() else ""

    if base in ("pwr", "getpwr"): return _resp_pwr(cmd)
    if base == "info":             return _resp_info(cmd)
    if base == "stat":             return _resp_stat(cmd)
    if base == "time":             return _resp_time(cmd)
    if base == "bat":              return _resp_bat(cmd)
    if base == "soh":              return _resp_soh(cmd)
    if base == "help":             return _resp_help(cmd)
    if base == "":                 return _JUST_PROMPT   # channel-prime newline

    # Unknown command — BMS-style error
    return f"{cmd}\r\nUnknown command '{base}'\r\n\r$$\r\n\rpylon>".encode("ascii")


# ---------------------------------------------------------------------------
# TCP server
# ---------------------------------------------------------------------------
class _BmsHandler(socketserver.StreamRequestHandler):
    """One handler instance per accepted TCP connection."""

    def handle(self) -> None:
        addr = f"{self.client_address[0]}:{self.client_address[1]}"
        print(f"[stub] + connected   {addr}")
        try:
            # Send initial prompt so the client knows the server is ready
            self.wfile.write(_PROMPT)
            self.wfile.flush()

            for raw_line in self.rfile:          # iterate lines (blocks until \n)
                response = _dispatch(raw_line.decode("ascii", errors="ignore"))
                self.wfile.write(response)
                self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            print(f"[stub] - disconnected {addr}")


class _StubServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threading so multiple HA instances can connect simultaneously."""
    allow_reuse_address = True
    daemon_threads      = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pylontech BMS RS232 TCP Stub Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--host",      default="127.0.0.1", metavar="HOST",
                    help="Bind address (default: 127.0.0.1)")
    ap.add_argument("--port",      default=12300, type=int, metavar="PORT",
                    help="TCP port (default: 12300)")
    ap.add_argument("--batteries", default=2, type=int, choices=range(1, 9),
                    metavar="N",
                    help="Number of battery modules present (1-8, default: 2)")
    ap.add_argument("--slots",     default=8, type=int, choices=range(1, 9),
                    metavar="N",
                    help="Total pwr table rows / slots (default: 8)")
    ap.add_argument("--model",     default="US2000", choices=list(MODELS),
                    help="Battery model: US2000 | US3000 | US5000 (default: US2000)")
    ap.add_argument("--soc",       default=85, type=int,
                    metavar="PCT",
                    help="Starting SOC %% (default: 85)")
    args = ap.parse_args()

    if args.batteries > args.slots:
        ap.error(f"--batteries ({args.batteries}) cannot exceed --slots ({args.slots})")

    _cfg["model"]     = args.model
    _cfg["batteries"] = args.batteries
    _cfg["slots"]     = args.slots
    _state["soc"]     = args.soc

    # Start background state updater
    threading.Thread(target=_state_updater, daemon=True).start()

    m = MODELS[args.model]
    print(f"[stub] Pylontech BMS stub")
    print(f"[stub]   address  : {args.host}:{args.port}")
    print(f"[stub]   model    : {args.model}  ({m['device_name']}, {m['spec']})")
    print(f"[stub]   batteries: {args.batteries} present / {args.slots} slots")
    print(f"[stub]   SOC start: {args.soc}%")
    print(f"[stub]   HA config: TCP Socket  {args.host}:{args.port}")
    print()

    with _StubServer((args.host, args.port), _BmsHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
