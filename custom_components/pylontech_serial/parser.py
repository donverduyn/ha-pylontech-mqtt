import logging
import re
from datetime import datetime

from .structs import PylontechBattery, PylontechSystem

_LOGGER = logging.getLogger(__name__)


class PylontechParser:
    """Parser for Pylontech BMS serial data."""

    @staticmethod
    def parse_pwr(raw_text: str, current_system: PylontechSystem | None = None) -> PylontechSystem:
        """Parses 'pwr' command output. Returns updated system object."""
        if current_system is None:
            current_system = PylontechSystem(0, 0, 0, 0, 0, 0, 0)

        batteries = []
        lines = raw_text.splitlines()

        # Detect column positions from the header line so the parser is robust
        # against firmware variations (e.g. US5000 vs US2000/US3000) that may
        # add, remove, or reorder columns.  Fall back to the known-good defaults
        # if no header is found (matches original behaviour).
        #
        # NOTE: "Time" in the header splits into two tokens (date + time) in
        # data rows.  Only columns that appear *before* Time are safe to look
        # up by header index; B.V.St / B.T.St are intentionally skipped here.
        volt_idx = 1
        curr_idx = 2
        temp_idx = 3
        temp_low_idx = 4
        temp_high_idx = 5
        volt_low_idx = 6
        volt_high_idx = 7
        status_idx = 8
        volt_st_idx = 9
        curr_st_idx = 10
        temp_st_idx = 11
        soc_idx = 12
        # B.V.St / B.T.St appear after the Time column; Time takes two tokens in
        # data rows (date + clock), so their data index = header index + 1.
        bvst_data_idx = 15
        btst_data_idx = 16

        for line in lines:
            parts = line.split()
            # Header line starts with "Power" and always contains "Coulomb"
            if parts and parts[0] == "Power" and "Coulomb" in parts:
                volt_idx = parts.index("Volt") if "Volt" in parts else volt_idx
                curr_idx = parts.index("Curr") if "Curr" in parts else curr_idx
                temp_idx = parts.index("Tempr") if "Tempr" in parts else temp_idx
                temp_low_idx = parts.index("Tlow") if "Tlow" in parts else temp_low_idx
                temp_high_idx = parts.index("Thigh") if "Thigh" in parts else temp_high_idx
                volt_low_idx = parts.index("Vlow") if "Vlow" in parts else volt_low_idx
                volt_high_idx = parts.index("Vhigh") if "Vhigh" in parts else volt_high_idx
                status_idx = parts.index("Base.St") if "Base.St" in parts else status_idx
                volt_st_idx = parts.index("Volt.St") if "Volt.St" in parts else volt_st_idx
                curr_st_idx = parts.index("Curr.St") if "Curr.St" in parts else curr_st_idx
                temp_st_idx = parts.index("Temp.St") if "Temp.St" in parts else temp_st_idx
                soc_idx = parts.index("Coulomb") if "Coulomb" in parts else soc_idx
                bvst_data_idx = (parts.index("B.V.St") + 1) if "B.V.St" in parts else bvst_data_idx
                btst_data_idx = (parts.index("B.T.St") + 1) if "B.T.St" in parts else btst_data_idx
                _LOGGER.debug(
                    "pwr header detected — volt=%d curr=%d temp=%d "
                    "tlow=%d thigh=%d vlow=%d vhigh=%d "
                    "status=%d volt_st=%d curr_st=%d temp_st=%d soc=%d",
                    volt_idx,
                    curr_idx,
                    temp_idx,
                    temp_low_idx,
                    temp_high_idx,
                    volt_low_idx,
                    volt_high_idx,
                    status_idx,
                    volt_st_idx,
                    curr_st_idx,
                    temp_st_idx,
                    soc_idx,
                )
                break

        # Helpers for parsing extended columns — defined once, used per row.
        def _mV(p: list, idx: int) -> float | None:
            """Return millivolt column as volts, or None if column absent or placeholder '-'."""
            if len(p) <= idx or p[idx] == "-":
                return None
            return int(p[idx]) / 1000.0

        def _st(p: list, idx: int) -> str | None:
            """Return status string column, or None if absent / placeholder '-'."""
            if len(p) <= idx:
                return None
            v = p[idx].strip()
            return v if v != "-" else None

        valid_lines = 0
        total_voltage = 0.0
        total_current = 0.0
        total_soc = 0.0

        for line in lines:
            parts = line.split()
            if len(parts) > 10 and parts[0].isdigit():
                if "Absent" in line:
                    continue
                try:
                    bat_id = int(parts[0])
                    voltage = int(parts[volt_idx]) / 1000.0
                    current = int(parts[curr_idx]) / 1000.0
                    temp = int(parts[temp_idx]) / 1000.0
                    status = parts[status_idx]
                    soc = int(parts[soc_idx].replace("%", ""))

                    # Extended fields — gracefully absent on older firmware
                    temp_low = _mV(parts, temp_low_idx)
                    temp_high = _mV(parts, temp_high_idx)
                    volt_low = _mV(parts, volt_low_idx)
                    volt_high = _mV(parts, volt_high_idx)
                    volt_st = _st(parts, volt_st_idx)
                    curr_st = _st(parts, curr_st_idx)
                    temp_st = _st(parts, temp_st_idx)
                    bvst = _st(parts, bvst_data_idx)
                    btst = _st(parts, btst_data_idx)

                    power = round(voltage * current, 2)

                    bat = PylontechBattery(
                        sys_id=bat_id,
                        voltage=voltage,
                        current=current,
                        temperature=temp,
                        soc=soc,
                        status=status,
                        power=power,
                        raw=line.strip(),
                        energy_stored=0.0,
                        temp_low=temp_low,
                        temp_high=temp_high,
                        volt_low=volt_low,
                        volt_high=volt_high,
                        volt_status=volt_st,
                        curr_status=curr_st,
                        temp_status=temp_st,
                        batt_volt_status=bvst,
                        batt_temp_status=btst,
                    )
                    batteries.append(bat)

                    total_voltage += voltage
                    total_current += current
                    total_soc += soc
                    valid_lines += 1

                except (ValueError, IndexError) as error:
                    _LOGGER.error(f"Error parsing pwr line '{line}': {error}")
                    continue

        current_system.batteries = batteries
        current_system.raw = raw_text

        if valid_lines > 0:
            current_system.voltage = round(total_voltage / valid_lines, 2)
            current_system.current = round(total_current, 2)
            current_system.soc = round(total_soc / valid_lines, 1)
            current_system.power = round(current_system.voltage * current_system.current, 1)

        return current_system

    @staticmethod
    def parse_info(raw_text: str, system: PylontechSystem) -> PylontechSystem:
        """Parses 'info' command output."""
        lines = raw_text.splitlines()
        for line in lines:
            if ":" not in line:
                continue
            parts = line.split(":", 1)
            # Normalise internal whitespace so "Soft  version" → "soft version"
            key = re.sub(r"\s+", " ", parts[0].strip().lower())
            val = parts[1].strip()

            if "manufacturer" in key:
                system.manufacturer = val
            if "device name" in key:
                system.model = val
            if "main soft" in key:
                system.fw_version = val
            if "board version" in key:
                system.board_version = val
            if key == "soft version":
                system.soft_version = val
            if key == "boot version":
                system.boot_version = val
            if "comm version" in key:
                system.comm_version = val
            if "release date" in key:
                system.release_date = val
            if "barcode" in key:
                system.barcode = val
            if "specification" in key:
                system.spec = val
            if "cell number" in key:
                try:
                    system.cell_count = int(val)
                except (ValueError, AttributeError):
                    pass
            if "max dischg curr" in key:
                try:
                    system.max_dischg_curr = abs(int(re.sub(r"[^\d-]", "", val))) / 1000.0
                except (ValueError, AttributeError):
                    pass
            if "max charge curr" in key:
                try:
                    system.max_charge_curr = int(re.sub(r"\D", "", val)) / 1000.0
                except (ValueError, AttributeError):
                    pass

        return system

    @staticmethod
    def parse_stat(raw_text: str, system: PylontechSystem) -> PylontechSystem:
        """Parses 'stat' command output."""

        def _int(pattern: str) -> int | None:
            m = re.search(pattern, raw_text, re.IGNORECASE)
            return int(m.group(1)) if m else None

        system.cycles = _int(r"CYCLE Times\s*:\s*(\d+)")
        system.charge_times = _int(r"Charge Times\s*:\s*(\d+)")
        system.discharge_cnt = _int(r"Discharge Cnt\.\s*:\s*(\d+)")
        system.idle_times = _int(r"Idle Times\s*:\s*(\d+)")
        system.shut_times = _int(r"Shut Times\s*:\s*(\d+)")
        system.reset_times = _int(r"Reset Times\s*:\s*(\d+)")
        system.sc_times = _int(r"SC Times\s*:\s*(\d+)")
        system.bat_ov_times = _int(r"Bat OV Times\s*:\s*(\d+)")
        system.bat_hv_times = _int(r"Bat HV Times\s*:\s*(\d+)")
        system.bat_lv_times = _int(r"Bat LV Times\s*:\s*(\d+)")
        system.bat_uv_times = _int(r"Bat UV Times\s*:\s*(\d+)")
        system.pwr_ov_times = _int(r"Pwr OV Times\s*:\s*(\d+)")
        system.pwr_hv_times = _int(r"Pwr HV Times\s*:\s*(\d+)")
        system.life_warn_times = _int(r"LifeWarn Times\s*:\s*(\d+)")
        system.life_alarm_times = _int(r"LifeAlarm Times\s*:\s*(\d+)")
        system.pwr_coulomb = _int(r"Pwr Coulomb\s*:\s*(\d+)")
        system.dsg_cap = _int(r"Dsg Cap\s*:\s*(\d+)")

        return system

    @staticmethod
    def parse_time(raw_text: str, system: PylontechSystem) -> PylontechSystem:
        """Parses 'time' command output.
        Example: Ds3231 2025-12-21 21:14:53
        """
        # Look for YYYY-MM-DD HH:MM:SS pattern
        match = re.search(r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})", raw_text)
        if match:
            system.bms_time = match.group(1)
        return system

    @staticmethod
    def generate_time_command(timestamp: datetime) -> str:
        """Generates the 'time' command for specific datetime."""
        # time [year] [month] [day] [hour] [minute] [second]
        # Example: time 25 12 21 13 00 00
        return timestamp.strftime("time %y %m %d %H %M %S")
