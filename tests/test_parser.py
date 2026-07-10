"""
Parser tests against the live TCP stub.

Every test in this file sends a real command to pylon_stub.py and verifies
that the parser correctly extracts every field it is responsible for.
"""

import re
from datetime import datetime

import pytest
from conftest import (
    STUB_BATTERIES,
    STUB_CELLS,
    STUB_MODEL,
    STUB_SOC_START,
    _raw_command,
)

from pylontech_parser import PylontechParser
from structs import PylontechBattery, PylontechSystem


# parse_pwr — power table
class TestParsePwr:
    def test_returns_system(self, pwr_system):
        assert isinstance(pwr_system, PylontechSystem)

    def test_correct_battery_count(self, pwr_system):
        """Only present (non-Absent) batteries should appear in the list."""
        assert len(pwr_system.batteries) == STUB_BATTERIES

    def test_battery_ids_sequential(self, pwr_system):
        ids = [b.sys_id for b in pwr_system.batteries]
        assert ids == list(range(1, STUB_BATTERIES + 1))

    def test_battery_voltage_positive(self, pwr_system):
        for bat in pwr_system.batteries:
            assert bat.voltage > 0, f"bat {bat.sys_id}: voltage should be > 0"

    def test_battery_current_nonzero(self, pwr_system):
        for bat in pwr_system.batteries:
            # stub charges with positive current
            assert bat.current != 0, f"bat {bat.sys_id}: current should be non-zero"

    def test_battery_temperature_plausible(self, pwr_system):
        for bat in pwr_system.batteries:
            assert 0 < bat.temperature < 80, (
                f"bat {bat.sys_id}: temperature {bat.temperature} out of plausible "
                "range"
            )

    def test_battery_soc_matches_stub_start(self, pwr_system):
        for bat in pwr_system.batteries:
            assert bat.soc == STUB_SOC_START

    def test_battery_status_string(self, pwr_system):
        valid_statuses = {"Charge", "Discharge", "Idle", "Normal"}
        for bat in pwr_system.batteries:
            assert bat.status in valid_statuses, (
                f"bat {bat.sys_id}: unexpected status '{bat.status}'"
            )

    def test_battery_power_calculated(self, pwr_system):
        for bat in pwr_system.batteries:
            expected = round(bat.voltage * bat.current, 2)
            assert bat.power == expected, (
                f"bat {bat.sys_id}: power mismatch "
                f"(got {bat.power}, expected {expected})"
            )

    # Extended pwr columns (Tlow/Thigh/Vlow/Vhigh)

    def test_cell_temp_low_present(self, pwr_system):
        for bat in pwr_system.batteries:
            assert bat.temp_low is not None, f"bat {bat.sys_id}: temp_low is None"

    def test_cell_temp_high_present(self, pwr_system):
        for bat in pwr_system.batteries:
            assert bat.temp_high is not None, f"bat {bat.sys_id}: temp_high is None"

    def test_cell_temp_ordering(self, pwr_system):
        for bat in pwr_system.batteries:
            assert bat.temp_low <= bat.temp_high, (
                f"bat {bat.sys_id}: temp_low ({bat.temp_low}) "
                f"> temp_high ({bat.temp_high})"
            )

    def test_cell_volt_low_present(self, pwr_system):
        for bat in pwr_system.batteries:
            assert bat.volt_low is not None, f"bat {bat.sys_id}: volt_low is None"

    def test_cell_volt_high_present(self, pwr_system):
        for bat in pwr_system.batteries:
            assert bat.volt_high is not None, f"bat {bat.sys_id}: volt_high is None"

    def test_cell_volt_ordering(self, pwr_system):
        for bat in pwr_system.batteries:
            assert bat.volt_low <= bat.volt_high, (
                f"bat {bat.sys_id}: volt_low ({bat.volt_low}) "
                f"> volt_high ({bat.volt_high})"
            )

    def test_cell_volt_plausible_range(self, pwr_system):
        """LiFePO4 cells: 2.5 V (deep discharge) – 3.65 V (full)."""
        for bat in pwr_system.batteries:
            assert 2.5 <= bat.volt_low <= 3.8, (
                f"bat {bat.sys_id}: volt_low {bat.volt_low} outside 2.5-3.8 V"
            )
            assert 2.5 <= bat.volt_high <= 3.8, (
                f"bat {bat.sys_id}: volt_high {bat.volt_high} outside 2.5-3.8 V"
            )

    # Status string columns (Volt.St / Curr.St / Temp.St)

    def test_status_strings_are_normal(self, pwr_system):
        for bat in pwr_system.batteries:
            assert bat.volt_status == "Normal"
            assert bat.curr_status == "Normal"
            assert bat.temp_status == "Normal"

    # B.V.St / B.T.St (battery-level voltage/temperature state)

    def test_batt_status_strings_are_normal(self, pwr_system):
        for bat in pwr_system.batteries:
            assert bat.batt_volt_status == "Normal"
            assert bat.batt_temp_status == "Normal"

    # System-level aggregates

    def test_system_voltage_is_average(self, pwr_system):
        avg = round(
            sum(b.voltage for b in pwr_system.batteries) / len(pwr_system.batteries),
            2,
        )
        assert pwr_system.voltage == avg

    def test_system_current_is_sum(self, pwr_system):
        total = round(sum(b.current for b in pwr_system.batteries), 2)
        assert pwr_system.current == total

    def test_system_soc_is_average(self, pwr_system):
        avg = round(
            sum(b.soc for b in pwr_system.batteries) / len(pwr_system.batteries),
            1,
        )
        assert pwr_system.soc == avg

    def test_system_power_calculated(self, pwr_system):
        """System power must equal the sum of per-battery powers (not V̄ × ΣI)."""
        expected = round(sum(b.power for b in pwr_system.batteries), 1)
        assert pwr_system.power == expected

    # Absent slot handling

    def test_absent_rows_excluded(self, stub_conn):
        """Absent battery slots must never appear in the battery list."""
        raw = _raw_command(stub_conn, "pwr")
        system = PylontechParser.parse_pwr(raw)
        assert all(b.status != "Absent" for b in system.batteries)

    # Header-based column detection (robustness)

    def test_reparse_stripped_header(self, stub_conn):
        """Parser must fall back to defaults when the header line is absent."""
        raw = _raw_command(stub_conn, "pwr")
        # Strip the header line to simulate old firmware without it
        lines = raw.splitlines()
        stripped = "\n".join(
            line for line in lines if not line.strip().startswith("Power")
        )
        system = PylontechParser.parse_pwr(stripped)
        # Should still parse the data rows (defaults kick in)
        assert len(system.batteries) == STUB_BATTERIES


# parse_pwr_indexed — vertical 'pwr N' block (fallback battery discovery)
class TestParsePwrIndexed:
    def test_valid_battery_returns_battery(self, _session_conn):
        raw = _raw_command(_session_conn, "pwr 1")
        bat = PylontechParser.parse_pwr_indexed(raw, 1)
        assert isinstance(bat, PylontechBattery)
        assert bat.sys_id == 1

    def test_voltage_current_temperature_scaled_from_milli(self, _session_conn):
        raw = _raw_command(_session_conn, "pwr 1")
        bat = PylontechParser.parse_pwr_indexed(raw, 1)
        assert bat is not None
        assert bat.voltage > 0
        assert bat.temperature > 0

    def test_soc_matches_stub_start(self, _session_conn):
        raw = _raw_command(_session_conn, "pwr 1")
        bat = PylontechParser.parse_pwr_indexed(raw, 1)
        assert bat is not None
        assert bat.soc == STUB_SOC_START

    def test_power_is_voltage_times_current(self, _session_conn):
        raw = _raw_command(_session_conn, "pwr 1")
        bat = PylontechParser.parse_pwr_indexed(raw, 1)
        assert bat is not None
        assert bat.power == pytest.approx(bat.voltage * bat.current, rel=1e-3)

    def test_status_fields_populated(self, _session_conn):
        raw = _raw_command(_session_conn, "pwr 1")
        bat = PylontechParser.parse_pwr_indexed(raw, 1)
        assert bat is not None
        assert bat.status
        assert bat.volt_status == "Normal"
        assert bat.curr_status == "Normal"
        assert bat.temp_status == "Normal"
        assert bat.coul_status == "Normal"

    def test_events_parsed_as_numbers(self, _session_conn):
        """Bat/Power Events are hex-formatted ("0x0") on the console; they
        must come back as ints via parse_number, not raw strings."""
        raw = _raw_command(_session_conn, "pwr 1")
        bat = PylontechParser.parse_pwr_indexed(raw, 1)
        assert bat is not None
        assert bat.bat_events == 0
        assert bat.power_events == 0
        assert bat.sys_fault == 0

    def test_absent_slot_returns_none(self, _session_conn):
        """Slot index beyond the configured battery count but within the
        BMS's own slot count (STUB_BATTERIES=2, slot 3 of 8) is Absent."""
        raw = _raw_command(_session_conn, "pwr 3")
        assert PylontechParser.parse_pwr_indexed(raw, 3) is None

    def test_out_of_range_returns_none(self, _session_conn):
        raw = _raw_command(_session_conn, "pwr 99")
        assert PylontechParser.parse_pwr_indexed(raw, 99) is None


# parse_number — decimal/hex event value helper
class TestParseNumber:
    def test_parses_decimal(self):
        assert PylontechParser.parse_number("42") == 42

    def test_parses_hex(self):
        assert PylontechParser.parse_number("0x10") == 16

    def test_parses_uppercase_hex_prefix(self):
        assert PylontechParser.parse_number("0X1F") == 31

    def test_dash_placeholder_returns_none(self):
        assert PylontechParser.parse_number("-") is None

    def test_empty_returns_none(self):
        assert PylontechParser.parse_number("") is None

    def test_garbage_returns_none(self):
        assert PylontechParser.parse_number("nope") is None


# parse_info — device information
class TestParseInfo:
    def test_manufacturer(self, info_system):
        assert info_system.manufacturer == "Pylon"

    def test_model_matches_stub_model(self, info_system):
        """Stub returns device_name matching the --model flag (US5000 → US5KBPL)."""
        model_map = {"US2000": "US2KBPL", "US3000": "US3KBPL", "US5000": "US5KBPL"}
        assert info_system.model == model_map[STUB_MODEL]

    def test_fw_version(self, info_system):
        assert info_system.fw_version is not None
        assert len(info_system.fw_version) > 0

    def test_soft_version(self, info_system):
        assert info_system.soft_version is not None

    def test_board_version(self, info_system):
        assert info_system.board_version is not None

    def test_boot_version(self, info_system):
        assert info_system.boot_version is not None

    def test_comm_version(self, info_system):
        assert info_system.comm_version is not None

    def test_release_date(self, info_system):
        assert info_system.release_date is not None

    def test_barcode(self, info_system):
        assert info_system.barcode is not None
        assert len(info_system.barcode) > 0

    def test_specification(self, info_system):
        """Specification should match the model (US5000 → 48V/100AH)."""
        spec_map = {"US2000": "48V/50AH", "US3000": "48V/74AH", "US5000": "48V/100AH"}
        assert info_system.spec == spec_map[STUB_MODEL]

    def test_cell_count(self, info_system):
        assert info_system.cell_count == 15

    def test_max_currents_match_model(self, info_system):
        """US5000 stub emits ±200 A limits."""
        limits = {
            "US2000": (102.0, 100.0),
            "US3000": (150.0, 150.0),
            "US5000": (200.0, 200.0),
        }
        chg, dsg = limits[STUB_MODEL]
        assert info_system.max_charge_curr == pytest.approx(chg, rel=1e-3)
        assert info_system.max_dischg_curr == pytest.approx(dsg, rel=1e-3)


# parse_stat — statistics and fault counters
class TestParseStat:
    def test_discharge_cnt(self, stat_system):
        assert stat_system.discharge_cnt is not None
        assert stat_system.discharge_cnt >= 0

    def test_idle_times(self, stat_system):
        assert stat_system.idle_times is not None
        assert stat_system.idle_times >= 0

    def test_sc_times(self, stat_system):
        assert stat_system.sc_times is not None
        assert stat_system.sc_times >= 0

    def test_bat_lv_times(self, stat_system):
        assert stat_system.bat_lv_times is not None

    def test_bat_uv_times(self, stat_system):
        assert stat_system.bat_uv_times is not None

    def test_stub_initial_values(self, stat_system):
        """Verify the stub seeds its counters with known values."""
        assert stat_system.cycles == 430
        assert stat_system.soh == 92  # max(0, 100 - int(430 * 0.02))
        assert stat_system.charge_times == 1150
        assert stat_system.shut_times == 329
        assert stat_system.reset_times == 67
        assert stat_system.bat_ov_times == 56
        assert stat_system.bat_hv_times == 5832
        assert stat_system.pwr_ov_times == 4688
        assert stat_system.pwr_hv_times == 6734
        assert stat_system.pwr_coulomb == 153311400
        assert stat_system.dsg_cap == 21506462
        assert stat_system.life_warn_times == 0
        assert stat_system.life_alarm_times == 0


# parse_time — BMS clock
class TestParseTime:
    def test_bms_time_present(self, time_system):
        assert time_system.bms_time is not None
        assert len(time_system.bms_time) > 0

    def test_bms_time_format(self, time_system):
        """Must match YYYY-MM-DD HH:MM:SS."""
        assert re.match(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
            time_system.bms_time,
        ), f"Unexpected bms_time format: '{time_system.bms_time}'"

    def test_bms_time_is_recent(self, time_system):
        """The stub returns the real wall clock so the time should be close to now."""
        parsed = datetime.strptime(time_system.bms_time, "%Y-%m-%d %H:%M:%S")
        delta = abs((datetime.now() - parsed).total_seconds())
        assert delta < 60, f"bms_time is {delta:.0f}s away from now"


# parse_pwr  ×  parse_stat  ×  parse_info in combination
# (simulates what the coordinator does on each full poll cycle)
class TestFullPollCycle:
    @pytest.fixture(scope="class")
    @classmethod
    def full_system(cls, stub_conn_class):
        """Run all three commands sequentially, as the coordinator does."""
        raw_pwr = _raw_command(stub_conn_class, "pwr")
        raw_stat = _raw_command(stub_conn_class, "stat")
        raw_time = _raw_command(stub_conn_class, "time")
        raw_info = _raw_command(stub_conn_class, "info")

        system = PylontechParser.parse_pwr(raw_pwr)
        PylontechParser.parse_stat(raw_stat, system)
        PylontechParser.parse_time(raw_time, system)
        PylontechParser.parse_info(raw_info, system)
        return system

    def test_batteries_and_stat_coexist(self, full_system):
        assert len(full_system.batteries) == STUB_BATTERIES
        assert full_system.cycles is not None

    def test_bms_time_populated(self, full_system):
        assert full_system.bms_time is not None

    def test_manufacturer_populated(self, full_system):
        assert full_system.manufacturer is not None

    def test_model_populated(self, full_system):
        assert full_system.model is not None

    def test_max_currents_populated(self, full_system):
        assert full_system.max_charge_curr is not None
        assert full_system.max_dischg_curr is not None

    def test_stat_fields_present(self, full_system):
        required = [
            "cycles",
            "charge_times",
            "discharge_cnt",
            "shut_times",
            "sc_times",
            "bat_ov_times",
            "bat_hv_times",
            "pwr_ov_times",
            "pwr_hv_times",
            "life_warn_times",
            "life_alarm_times",
            "pwr_coulomb",
            "dsg_cap",
        ]
        for field in required:
            assert getattr(full_system, field) is not None, (
                f"{field} is None after full cycle"
            )

    def test_all_battery_extended_fields_populated(self, full_system):
        for bat in full_system.batteries:
            assert bat.volt_low is not None, f"bat {bat.sys_id}: volt_low None"
            assert bat.volt_high is not None, f"bat {bat.sys_id}: volt_high None"
            assert bat.temp_low is not None, f"bat {bat.sys_id}: temp_low None"
            assert bat.temp_high is not None, f"bat {bat.sys_id}: temp_high None"
            assert bat.volt_status is not None, f"bat {bat.sys_id}: volt_status None"
            assert bat.curr_status is not None, f"bat {bat.sys_id}: curr_status None"
            assert bat.temp_status is not None, f"bat {bat.sys_id}: temp_status None"
            assert bat.batt_volt_status is not None, (
                f"bat {bat.sys_id}: batt_volt_status None"
            )
            assert bat.batt_temp_status is not None, (
                f"bat {bat.sys_id}: batt_temp_status None"
            )


# Stub protocol parity — raw response format checks
class TestStubProtocolParity:
    """Verify that the stub responses contain the structural markers that the
    coordinator and parsers rely on, without going through the parser layer."""

    @pytest.fixture(scope="class")
    @classmethod
    def raw(cls, stub_conn_class):
        cmds = ["pwr", "info", "stat", "time", "bat", "soh", "help"]
        return {c: _raw_command(stub_conn_class, c) for c in cmds}

    def test_all_responses_end_with_prompt(self, raw):
        for cmd, resp in raw.items():
            assert "pylon>" in resp, f"'{cmd}' response missing 'pylon>' prompt"

    def test_all_responses_have_completion_marker(self, raw):
        for cmd, resp in raw.items():
            assert "Command completed successfully" in resp, (
                f"'{cmd}' response missing completion marker"
            )

    def test_pwr_has_header_columns(self, raw):
        for col in (
            "Power",
            "Volt",
            "Curr",
            "Tempr",
            "Tlow",
            "Thigh",
            "Vlow",
            "Vhigh",
            "Base.St",
            "Coulomb",
            "B.V.St",
            "B.T.St",
        ):
            assert col in raw["pwr"], f"pwr response missing column '{col}'"

    def test_pwr_has_absent_rows(self, raw):
        assert "Absent" in raw["pwr"]

    def test_info_has_required_keys(self, raw):
        for key in (
            "Manufacturer",
            "Device name",
            "Main Soft version",
            "Barcode",
            "Specification",
            "Cell Number",
            "Max Dischg Curr",
            "Max Charge Curr",
        ):
            assert key in raw["info"], f"info response missing key '{key}'"

    def test_stat_has_required_counters(self, raw):
        for key in (
            "CYCLE Times",
            "Charge Times",
            "SC Times",
            "Bat OV Times",
            "Bat HV Times",
            "LifeWarn Times",
            "LifeAlarm Times",
            "Pwr Coulomb",
            "Dsg Cap",
        ):
            assert key in raw["stat"], f"stat response missing counter '{key}'"

    def test_time_has_ds3231_prefix(self, raw):
        assert "Ds3231" in raw["time"]

    def test_time_has_datetime_pattern(self, raw):
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", raw["time"])

    def test_bat_has_cell_rows(self, raw):
        assert "Battery" in raw["bat"]
        assert "mAH" in raw["bat"]

    def test_soh_has_cell_rows(self, raw):
        assert "SOHStatus" in raw["soh"]
        assert "Normal" in raw["soh"]

    def test_help_lists_key_commands(self, raw):
        for cmd in ("pwr", "info", "stat", "time", "bat", "soh"):
            assert cmd in raw["help"], f"help response does not list '{cmd}'"

    def test_unknown_command_returns_error(self, stub_conn):
        resp = _raw_command(stub_conn, "notacommand")
        assert "Unknown command" in resp
        assert "pylon>" in resp

    def test_bare_newline_returns_prompt(self, stub_conn):
        resp = _raw_command(stub_conn, "")
        assert "pylon>" in resp

    def test_time_set_acknowledged(self, stub_conn):
        resp = _raw_command(stub_conn, "time 25 06 30 12 00 00")
        assert "Command completed successfully" in resp
        assert "pylon>" in resp


# Edge-case / error-path tests  (pure unit — no stub required)
# These cover the defensive branches that the stub cannot trigger:
#   • _mV() → None  (column absent or "-" placeholder)
#   • _st() → None  (column out-of-bounds)
#   • except block in parse_pwr data loop  (corrupt row)
#   • except: pass blocks in parse_info   (non-numeric values)
#   • generate_time_command               (never called via stub)
class TestParsePwrEdgeCases:
    # _mV returns None for "-" placeholder in extended columns

    def test_mv_returns_none_for_dash_placeholder(self):
        """A present battery row where Tlow/Thigh/Vlow/Vhigh are "-" must still
        parse the mandatory fields and leave the optional ones as None."""
        raw = (
            "pwr\r\n@\r\r\n"
            "Power Volt   Curr   Tempr  Tlow   Thigh  Vlow   Vhigh  Base.St  "
            "Volt.St  Curr.St  Temp.St  Coulomb  Time                 B.V.St   B.T.St  "
            "\r\r\n"
            "1     50691  3806   17000  -      -      -      -      Charge   "
            "Normal   Normal   Normal   89%      2025-12-21 20:53:06  Normal   Normal  "
            "\r\r\n"
            "Command completed successfully\r\n\r$$\r\npylon>"
        )
        system = PylontechParser.parse_pwr(raw)
        assert len(system.batteries) == 1
        bat = system.batteries[0]
        assert bat.voltage == pytest.approx(50.691)
        assert bat.soc == 89
        assert bat.temp_low is None, "Tlow '-' should yield None"
        assert bat.temp_high is None, "Thigh '-' should yield None"
        assert bat.volt_low is None, "Vlow '-' should yield None"
        assert bat.volt_high is None, "Vhigh '-' should yield None"

    # _mV returns None when row is too short (column index out of bounds)

    def test_mv_returns_none_for_short_row(self):
        """A row that ends before the extended columns must leave them as None."""
        raw = (
            "pwr\r\n@\r\r\n"
            "Power Volt   Curr   Tempr  Tlow   Thigh  Vlow   Vhigh  Base.St  "
            "Volt.St  Curr.St  Temp.St  Coulomb\r\r\n"
            # Only 13 tokens — no Time, B.V.St, B.T.St columns
            "1     50691  3806   17000  13000  14000  3378   3381   Charge   "
            "Normal   Normal   Normal   75%\r\r\n"
            "Command completed successfully\r\npylon>"
        )
        system = PylontechParser.parse_pwr(raw)
        assert len(system.batteries) == 1
        bat = system.batteries[0]
        assert bat.soc == 75
        # bvst/btst are at index 15/16 — absent from this short row
        assert bat.batt_volt_status is None
        assert bat.batt_temp_status is None

    # _st returns None when row is too short

    def test_st_returns_none_for_short_row(self):
        """Volt.St/Curr.St/Temp.St absent → None, mandatory fields still parsed."""
        # Only 9 tokens: ID Volt Curr Tempr Tlow Thigh Vlow Vhigh Base.St
        # Status string columns (indices 9,10,11) are missing
        raw = (
            "pwr\r\n@\r\r\n"
            "Power Volt   Curr   Tempr  Tlow   Thigh  Vlow   Vhigh  Base.St  Coulomb"
            "\r\r\n"
            "1     50000  2000   17000  12000  15000  3300   3350   Charge   60%\r\r\n"
            "Command completed successfully\r\npylon>"
        )
        system = PylontechParser.parse_pwr(raw)
        # The row has 10 tokens, len(parts) > 10 is False → no battery parsed
        # (our guard requires > 10 tokens)
        assert len(system.batteries) == 0

    # except (ValueError, IndexError) block

    def test_corrupt_row_is_skipped_with_error_log(self, caplog):
        """A row whose voltage field is non-numeric must be skipped, not crash."""
        import logging

        raw = (
            "pwr\r\n@\r\r\n"
            "Power Volt   Curr   Tempr  Tlow   Thigh  Vlow   Vhigh  Base.St  "
            "Volt.St  Curr.St  Temp.St  Coulomb  Time                 B.V.St   B.T.St"
            "\r\r\n"
            # 'XXXX' is not a valid integer for voltage
            "1     XXXX   3806   17000  13000  14000  3378   3381   Charge   "
            "Normal   Normal   Normal   89%      2025-12-21 20:53:06  Normal   Normal"
            "\r\r\n"
            # A valid row follows to prove parsing continues after the error
            "2     50691  3806   17000  13000  14000  3378   3381   Charge   "
            "Normal   Normal   Normal   89%      2025-12-21 20:53:06  Normal   Normal"
            "\r\r\n"
            "Command completed successfully\r\npylon>"
        )
        with caplog.at_level(logging.ERROR):
            system = PylontechParser.parse_pwr(raw)

        # Bad row skipped, good row parsed
        assert len(system.batteries) == 1
        assert system.batteries[0].sys_id == 2
        # Error was logged
        assert any("Error parsing pwr line" in r.message for r in caplog.records)

    # parse_pwr with no batteries at all

    def test_empty_pwr_response(self):
        """All rows Absent → empty battery list, system defaults to zeros."""
        raw = (
            "pwr\r\n@\r\r\n"
            "Power Volt   Curr   Tempr  Tlow   Thigh  Vlow   Vhigh  Base.St  "
            "Volt.St  Curr.St  Temp.St  Coulomb\r\r\n"
            "1     -      -      -      -      -      -      -      Absent   "
            "-        -        -        -\r\r\n"
            "Command completed successfully\r\npylon>"
        )
        system = PylontechParser.parse_pwr(raw)
        assert len(system.batteries) == 0
        assert system.voltage == 0
        assert system.current == 0

    # parse_pwr with existing system object

    def test_parse_pwr_updates_existing_system(self):
        """Passing an existing PylontechSystem must update its batteries in-place."""
        existing = PylontechSystem(
            voltage=99,
            current=99,
            soc=99,
            power=99,
            energy_in=10.0,
            energy_out=5.0,
            energy_stored=50.0,
        )
        raw = (
            "pwr\r\n@\r\r\n"
            "Power Volt   Curr   Tempr  Tlow   Thigh  Vlow   Vhigh  Base.St  "
            "Volt.St  Curr.St  Temp.St  Coulomb  Time                 B.V.St   B.T.St"
            "\r\r\n"
            "1     50000  3000   16000  12000  13000  3340   3360   Charge   "
            "Normal   Normal   Normal   80%      2025-12-21 20:53:06  Normal   Normal"
            "\r\r\n"
            "Command completed successfully\r\npylon>"
        )
        result = PylontechParser.parse_pwr(raw, existing)
        assert result is existing, "must return the same object"
        assert len(result.batteries) == 1
        assert result.batteries[0].soc == 80
        # Energy counters must be preserved
        assert result.energy_in == 10.0
        assert result.energy_out == 5.0


class TestParseInfoEdgeCases:
    # except: pass for non-numeric cell_count

    def test_non_numeric_cell_count_ignored(self):
        raw = (
            "info\r\n@\r\n"
            "Cell Number         : UNKNOWN\r\n"
            "Manufacturer        : Pylon\r\n"
            "Command completed successfully\r\npylon>"
        )
        system = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
        PylontechParser.parse_info(raw, system)
        assert system.cell_count is None, "Non-numeric cell_count must remain None"
        assert system.manufacturer == "Pylon"

    # except: pass for non-numeric max_dischg_curr

    def test_non_numeric_max_dischg_curr_ignored(self):
        raw = (
            "info\r\n@\r\n"
            "Max Dischg Curr     : N/A\r\n"
            "Max Charge Curr     : N/A\r\n"
            "Command completed successfully\r\npylon>"
        )
        system = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
        PylontechParser.parse_info(raw, system)
        assert system.max_dischg_curr is None
        assert system.max_charge_curr is None

    # empty info response

    def test_empty_info_response(self):
        system = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
        PylontechParser.parse_info("", system)
        assert system.manufacturer is None
        assert system.model is None

    # info without colon lines (no key-value content)

    def test_info_no_colon_lines(self):
        raw = "pylon>\r\nCommand completed successfully\r\n"
        system = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
        PylontechParser.parse_info(raw, system)
        assert system.model is None


class TestParseTimeEdgeCases:
    # no datetime pattern in response → bms_time stays None

    def test_no_match_leaves_bms_time_none(self):
        raw = "time\r\n@\r\nCommand completed successfully\r\npylon>"
        system = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
        PylontechParser.parse_time(raw, system)
        assert system.bms_time is None

    # generate_time_command produces correct format

    def test_generate_time_command_format(self):
        dt = datetime(2025, 12, 21, 13, 5, 9)
        cmd = PylontechParser.generate_time_command(dt)
        assert cmd == "time 25 12 21 13 05 09"

    def test_generate_time_command_zero_padded(self):
        dt = datetime(2026, 1, 7, 8, 3, 1)
        cmd = PylontechParser.generate_time_command(dt)
        assert cmd == "time 26 01 07 08 03 01"


class TestParseStatEdgeCases:
    # missing counters stay None

    def test_missing_counter_stays_none(self):
        """Stat text that omits some counters → those fields must be None."""
        raw = "stat\r\n@\r\nCYCLE Times     :      100\r\nCommand completed\r\n"
        system = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
        PylontechParser.parse_stat(raw, system)
        assert system.cycles == 100
        assert system.charge_times is None
        assert system.sc_times is None
        assert system.pwr_coulomb is None

    # empty stat response

    def test_empty_stat_response(self):
        system = PylontechSystem(0, 0, 0, 0, 0, 0, 0)
        PylontechParser.parse_stat("", system)
        assert system.cycles is None

    def test_battery_dataclass_optional_fields_default_to_none(self):
        bat = PylontechBattery(1, 50.0, 3.0, 25.0, 85, "Charge", 150.0, 0.0)
        assert bat.temp_low is None
        assert bat.temp_high is None
        assert bat.volt_low is None
        assert bat.volt_high is None
        assert bat.volt_status is None
        assert bat.curr_status is None
        assert bat.temp_status is None
        assert bat.batt_volt_status is None
        assert bat.batt_temp_status is None

    def test_battery_cells_default_to_empty_list(self):
        bat = PylontechBattery(1, 50.0, 3.0, 25.0, 85, "Charge", 150.0, 0.0)
        assert bat.cells == []


# parse_bat — per-cell data
class TestParseBat:
    def test_cell_count_matches_model(self, bat_battery):
        assert len(bat_battery.cells) == STUB_CELLS

    def test_cell_ids_sequential(self, bat_battery):
        ids = [c.cell_id for c in bat_battery.cells]
        assert ids == list(range(STUB_CELLS))

    def test_cell_voltage_plausible(self, bat_battery):
        """LiFePO4 cells: 2.5 V (deep discharge) – 3.8 V (max)."""
        for cell in bat_battery.cells:
            assert 2.5 <= cell.voltage <= 3.8, (
                f"cell {cell.cell_id}: voltage {cell.voltage} outside 2.5-3.8 V"
            )

    def test_cell_temperature_plausible(self, bat_battery):
        for cell in bat_battery.cells:
            assert 0 < cell.temperature < 80, (
                f"cell {cell.cell_id}: temperature {cell.temperature} out of range"
            )

    def test_cell_soc_matches_stub(self, bat_battery):
        for cell in bat_battery.cells:
            assert cell.soc == STUB_SOC_START

    def test_cell_base_state_valid(self, bat_battery):
        valid = {"Charge", "Dischg", "Idle", "Normal"}
        for cell in bat_battery.cells:
            assert cell.base_state in valid, (
                f"cell {cell.cell_id}: unexpected base_state '{cell.base_state}'"
            )

    def test_cell_statuses_are_normal(self, bat_battery):
        for cell in bat_battery.cells:
            assert cell.volt_status == "Normal"
            assert cell.curr_status == "Normal"
            assert cell.temp_status == "Normal"

    def test_cell_capacity_present(self, bat_battery):
        for cell in bat_battery.cells:
            assert cell.capacity is not None, f"cell {cell.cell_id}: capacity is None"
            assert cell.capacity > 0

    def test_absent_battery_has_no_cells(self, stub_conn):
        """Requesting 'bat N' for an absent/unknown slot must yield no cells."""
        from pylontech_parser import PylontechParser
        from structs import PylontechBattery

        # Slot 99 does not exist in the stub → "Battery 99 not found" response
        raw = _raw_command(stub_conn, "bat 99")
        bat = PylontechBattery(99, 0, 0, 0, 0, "", 0, 0.0)
        PylontechParser.parse_bat(raw, bat)
        assert bat.cells == []

    def test_corrupt_cell_row_skipped(self, caplog):
        """A non-numeric voltage in a cell row must be skipped, not crash."""
        import logging

        from pylontech_parser import PylontechParser
        from structs import PylontechBattery

        raw = (
            "bat 1\r\n@\r\r\n"
            "Battery  Volt     Curr     Tempr    Base State   "
            "Volt. State  Curr. State  Temp. State  SOC        Coulomb\r\r\n"
            "0        XXXX     3806     17000    Charge       "
            "Normal       Normal       Normal       85%        3333 mAH\r\r\n"
            "1        3379     3806     17100    Charge       "
            "Normal       Normal       Normal       85%        3333 mAH\r\r\n"
            "Command completed successfully\r\npylon>"
        )
        bat = PylontechBattery(1, 0, 0, 0, 0, "", 0, 0.0)
        with caplog.at_level(logging.ERROR):
            PylontechParser.parse_bat(raw, bat)

        assert len(bat.cells) == 1
        assert bat.cells[0].cell_id == 1
        assert any("Error parsing bat line" in r.message for r in caplog.records)

    def test_missing_soc_header_shifts_capacity_index(self):
        """Some Pytes/Pylontech firmware omits the "SOC" header label even
        though each data row still carries a percentage token immediately
        before the Coulomb (mAh) value. Without shifting cap_idx to account
        for the missing header token, int(parts[cap_idx]) reads "67%" and
        raises ValueError, silently dropping the row.
        """
        from pylontech_parser import PylontechParser
        from structs import PylontechBattery

        raw = (
            "bat 1\r\n@\r\r\n"
            "Battery  Volt     Curr     Tempr    Base State   "
            "Volt. State  Curr. State  Temp. State  Coulomb\r\r\n"
            "0        3378     3806     17000    Charge       "
            "Normal       Normal       Normal       67%      3333 mAH\r\r\n"
            "Command completed successfully\r\npylon>"
        )
        bat = PylontechBattery(1, 0, 0, 0, 0, "", 0, 0.0)
        PylontechParser.parse_bat(raw, bat)

        assert len(bat.cells) == 1
        cell = bat.cells[0]
        assert cell.soc == 67
        assert cell.capacity == 3333

    def test_all_absent_zeroes_system_metrics(self):
        """When every battery row is Absent, system voltage/current/soc/power
        must be reset to 0 rather than retaining stale previous values.

        Without the fix, parse_pwr only enters the ``if valid_lines > 0:``
        branch, leaving the previous values on the system object intact while
        setting batteries = []  — an inconsistent state.
        """
        from pylontech_parser import PylontechParser
        from structs import PylontechSystem

        raw = (
            "pwr\r\n"
            "Power  Volt   Curr   Tempr  Tlow   Thigh  Vlow   Vhigh  "
            "Base.St Volt.St Curr.St Temp.St Coulomb Time           B.V.St B.T.St\r\n"
            "1      50200  10000  25000  24500  25500  3350   3360   "
            "Absent  Normal  Normal  Normal  80%    2025-01-01 12:00:00  Normal Normal"
            "\r\n"
            "2      50100  9900   25100  24600  25400  3340   3370   "
            "Absent  Normal  Normal  Normal  79%    2025-01-01 12:00:00  Normal Normal"
            "\r\n"
            "Command completed successfully\r\npylon>"
        )
        # Start with a system that already has non-zero totals from a previous poll.
        system = PylontechSystem(51.0, 20.0, 80.0, 1020.0, 0.0, 0.0, 0.0)
        PylontechParser.parse_pwr(raw, system)

        assert system.batteries == []
        assert system.voltage == 0.0
        assert system.current == 0.0
        assert system.soc == 0.0
        assert system.power == 0.0
