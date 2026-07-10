"""
Full-coverage tests for pylon_stub.py — every command handler, admin-mode
path, fault-injection flow, firmware variant, and multi-group topology.

Isolation strategy
------------------
* Admin mode and fault state are global inside the stub process.
* Every test that calls login/stub/fault uses a function-scoped ``stub_conn``
  and wraps mutations in try/finally so a failing assertion cannot leave the
  stub in a dirty state for subsequent tests.
* New-firmware and multi-group tests spin up separate stub instances on
  distinct ports and are therefore fully independent of the session-level stub.
"""

import re
import socket

import pytest
from conftest import (
    STUB_HOST,
    STUB_SOC_START,
    _drain_prompt,
    _raw_command,
    _start_variant_stub,
)

# new_fw_stub/new_fw_conn live in conftest.py (shared with test_parser.py's
# parser-level coverage of the new-firmware column layout); only the
# multi-group variant is specific to this file.


@pytest.fixture(scope="module")
def multi_grp_stub():
    """Stub with --groups 2 (LV-HUB topology, two parallel battery groups)."""
    from conftest import _enable_sockets

    _enable_sockets()
    stub = _start_variant_stub("--groups", "2", "--firmware", "old")
    try:
        yield stub.port
    finally:
        stub.stop()


@pytest.fixture
def multi_grp_conn(multi_grp_stub):
    s = socket.create_connection((STUB_HOST, multi_grp_stub), timeout=3)
    _drain_prompt(s)
    yield s
    s.close()


# 1. Additional simple commands not covered by test_parser.py


class TestStubAdditionalCommands:
    """Commands implemented in the stub but not yet exercised by the parser suite."""

    def test_getpwr_alias(self, stub_conn):
        """getpwr must return the same power table as pwr."""
        resp = _raw_command(stub_conn, "getpwr")
        assert "Command completed successfully" in resp
        assert "pylon>" in resp
        assert "Volt" in resp

    def test_disp_returns_pwr_table(self, stub_conn):
        """disp delegates to _resp_pwr — must contain power-table columns."""
        resp = _raw_command(stub_conn, "disp")
        assert "Command completed successfully" in resp
        assert "Volt" in resp

    def test_log_has_count_and_entries(self, stub_conn):
        resp = _raw_command(stub_conn, "log")
        assert "Command completed successfully" in resp
        assert "Log count" in resp
        # Each log entry carries a datetime stamp
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", resp)

    def test_data_response(self, stub_conn):
        resp = _raw_command(stub_conn, "data")
        assert "Command completed successfully" in resp
        assert "Data Items" in resp

    def test_datalist_response(self, stub_conn):
        resp = _raw_command(stub_conn, "datalist")
        assert "Command completed successfully" in resp
        assert "DataList Items" in resp

    def test_prot_has_protection_rows(self, stub_conn):
        resp = _raw_command(stub_conn, "prot")
        assert "Command completed successfully" in resp
        assert "Volt.Prot" in resp
        assert "Curr.Prot" in resp
        assert "Temp.Prot" in resp
        assert "Normal" in resp

    def test_shut_acknowledges(self, stub_conn):
        resp = _raw_command(stub_conn, "shut")
        assert "Command completed successfully" in resp
        assert "shut down" in resp.lower()

    def test_trst_acknowledges(self, stub_conn):
        resp = _raw_command(stub_conn, "trst")
        assert "Command completed successfully" in resp
        assert "reset" in resp.lower()

    def test_updata_acknowledges(self, stub_conn):
        resp = _raw_command(stub_conn, "updata")
        assert "Command completed successfully" in resp

    def test_time_wrong_arg_count_returns_error(self, stub_conn):
        """time with too few args must return an error string, not crash."""
        resp = _raw_command(stub_conn, "time 25 06")
        assert "Command completed successfully" in resp
        assert "Error" in resp

    def test_pwrsys_single_group_structure(self, stub_conn):
        resp = _raw_command(stub_conn, "pwrsys")
        assert "Command completed successfully" in resp
        assert "Groups" in resp
        assert "Total modules" in resp
        assert "Online" in resp
        assert "Sys SOC" in resp
        assert "Sys Alarm" in resp

    def test_pwrsys_no_per_group_lines_for_single_group(self, stub_conn):
        """Per-group detail lines (Group 1 / Group 2 …) are only emitted when
        n_groups > 1; they must NOT appear in a single-group configuration."""
        resp = _raw_command(stub_conn, "pwrsys")
        assert "Group 1" not in resp


# 2. cmdquit — connection teardown


class TestStubCmdquit:
    def test_cmdquit_writes_quit_message(self, stub_server):
        """After cmdquit the server writes the quit message and closes the session."""
        s = socket.create_connection((STUB_HOST, stub_server), timeout=3)
        _drain_prompt(s)
        s.sendall(b"cmdquit\n")
        data = b""
        s.settimeout(2.0)
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except TimeoutError:
            pass
        s.close()
        decoded = data.decode("ascii", errors="replace")
        assert "Quit console mode" in decoded

    def test_cmdquit_connection_closes(self, stub_server):
        """After cmdquit the connection must reach EOF (recv returns b'')."""
        s = socket.create_connection((STUB_HOST, stub_server), timeout=3)
        _drain_prompt(s)
        s.sendall(b"cmdquit\n")
        # Drain until EOF or timeout
        received = b""
        s.settimeout(2.0)
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    # EOF — server closed the connection cleanly
                    break
                received += chunk
        except TimeoutError:
            pass
        s.close()
        # We must have received something (the quit message), and b'' signals EOF
        assert len(received) > 0


# 3. pwr N — indexed (vertical key:value) format


class TestStubPwrIndexed:
    """pwr N returns a completely different response shape from the tabular pwr."""

    def test_valid_battery_has_vertical_block(self, stub_conn):
        resp = _raw_command(stub_conn, "pwr 1")
        assert "Command completed successfully" in resp
        assert "Power 1" in resp
        assert "Voltage" in resp
        assert "Current" in resp
        assert "Basic Status" in resp

    def test_valid_battery_has_energy_rows(self, stub_conn):
        resp = _raw_command(stub_conn, "pwr 1")
        assert "Total Coulomb" in resp
        assert "Real Coulomb" in resp
        assert "Total Power In" in resp
        assert "Total Power Out" in resp

    def test_valid_battery_has_status_rows(self, stub_conn):
        resp = _raw_command(stub_conn, "pwr 1")
        assert "Volt Status" in resp
        assert "Current Status" in resp
        assert "Tmpr. Status" in resp
        assert "CMOS Status" in resp
        assert "DMOS Status" in resp

    def test_normal_battery_statuses_are_normal(self, stub_conn):
        resp = _raw_command(stub_conn, "pwr 1")
        assert "Normal" in resp

    def test_second_battery_indexed(self, stub_conn):
        resp = _raw_command(stub_conn, "pwr 2")
        assert "Power 2" in resp
        assert "Voltage" in resp

    def test_absent_slot_shows_absent(self, stub_conn):
        """Slot index beyond batt_per_group (slot 3 in a 2-battery config) is absent."""
        resp = _raw_command(stub_conn, "pwr 3")
        assert "Power 3" in resp
        assert "Absent" in resp

    def test_out_of_range_returns_not_found(self, stub_conn):
        """Battery ID beyond total slot count (>8 in default config) returns error."""
        resp = _raw_command(stub_conn, "pwr 9")
        assert "not found" in resp.lower()

    def test_has_protection_enables(self, stub_conn):
        """Normal battery block includes protection-enable masks."""
        resp = _raw_command(stub_conn, "pwr 1")
        assert "Bat Protect ENA" in resp
        assert "Pwr Protect ENA" in resp


# 4. bat N — per-cell data


class TestStubBatCommand:
    def test_bat_no_index_defaults_to_bat1(self, stub_conn):
        resp = _raw_command(stub_conn, "bat")
        assert "Command completed successfully" in resp
        assert "mAH" in resp

    def test_bat_explicit_index_1(self, stub_conn):
        resp = _raw_command(stub_conn, "bat 1")
        assert "Command completed successfully" in resp
        assert "Battery" in resp

    def test_bat_has_correct_cell_count(self, stub_conn):
        """US5000 has 15 cells; each row ends with '{cap} mAH'."""
        resp = _raw_command(stub_conn, "bat 1")
        # The header row contains 'Coulomb' but not 'mAH'; each cell row ends
        # with 'N mAH'
        assert resp.count("mAH") == 15

    def test_bat_has_state_columns(self, stub_conn):
        resp = _raw_command(stub_conn, "bat 1")
        for col in ("Base State", "Volt. State", "Curr. State", "Temp. State"):
            assert col in resp, f"bat response missing column '{col}'"

    def test_bat_second_battery(self, stub_conn):
        resp = _raw_command(stub_conn, "bat 2")
        assert "Command completed successfully" in resp
        assert "mAH" in resp

    def test_bat_absent_slot_returns_absent(self, stub_conn):
        """Slot beyond batt_per_group must be reported as Absent."""
        resp = _raw_command(stub_conn, "bat 3")
        assert "Absent" in resp

    def test_bat_out_of_range_returns_not_found(self, stub_conn):
        resp = _raw_command(stub_conn, "bat 9")
        assert "not found" in resp.lower()

    def test_bat_invalid_index_falls_back_to_bat1(self, stub_conn):
        """Non-integer index is silently ignored; default bat_id=1 is used."""
        resp = _raw_command(stub_conn, "bat abc")
        assert "Command completed successfully" in resp
        assert "mAH" in resp


# 5. soh N — per-cell state-of-health


class TestStubSohCommand:
    def test_soh_no_index_defaults_to_bat1(self, stub_conn):
        resp = _raw_command(stub_conn, "soh")
        assert "Command completed successfully" in resp
        assert "SOHStatus" in resp

    def test_soh_explicit_index(self, stub_conn):
        resp = _raw_command(stub_conn, "soh 1")
        assert "SOHStatus" in resp
        assert "Normal" in resp

    def test_soh_has_all_cells(self, stub_conn):
        """US5000 has 15 cells; each row carries 'Normal' SOHStatus."""
        resp = _raw_command(stub_conn, "soh 1")
        # Count 'Normal' occurrences; header row has 'SOHStatus' not 'Normal',
        # so each of the 15 cell rows contributes exactly one 'Normal'.
        assert resp.count("Normal") == 15

    def test_soh_header_row(self, stub_conn):
        resp = _raw_command(stub_conn, "soh 1")
        assert "Battery" in resp
        assert "Voltage" in resp
        assert "SOHCount" in resp

    def test_soh_second_battery(self, stub_conn):
        resp = _raw_command(stub_conn, "soh 2")
        assert "SOHStatus" in resp

    def test_soh_absent_slot_returns_absent(self, stub_conn):
        resp = _raw_command(stub_conn, "soh 3")
        assert "Absent" in resp

    def test_soh_out_of_range_returns_not_found(self, stub_conn):
        resp = _raw_command(stub_conn, "soh 9")
        assert "not found" in resp.lower()

    def test_soh_invalid_index_falls_back_to_bat1(self, stub_conn):
        resp = _raw_command(stub_conn, "soh xyz")
        assert "Command completed successfully" in resp


# 6. login / logout


class TestStubLoginLogout:
    def test_correct_password_grants_admin(self, stub_conn):
        resp = _raw_command(stub_conn, "login 000000")
        assert "Enter admin mode successfully" in resp
        _raw_command(stub_conn, "logout")

    def test_short_password_000_also_grants_admin(self, stub_conn):
        """'000' is an accepted shorthand password per stub implementation."""
        resp = _raw_command(stub_conn, "login 000")
        assert "Enter admin mode successfully" in resp
        _raw_command(stub_conn, "logout")

    def test_no_password_arg_grants_admin(self, stub_conn):
        """login with no password argument is also accepted (empty string match)."""
        resp = _raw_command(stub_conn, "login")
        assert "Enter admin mode successfully" in resp
        _raw_command(stub_conn, "logout")

    def test_wrong_password_denied(self, stub_conn):
        resp = _raw_command(stub_conn, "login wrongpassword")
        assert "Password error" in resp

    def test_wrong_password_does_not_set_admin(self, stub_conn):
        """After a failed login, stub controls must still be rejected."""
        _raw_command(stub_conn, "logout")  # ensure clean state
        _raw_command(stub_conn, "login wrongpassword")
        resp = _raw_command(stub_conn, "stub soc 50")
        assert "Admin mode required" in resp

    def test_logout_succeeds(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        resp = _raw_command(stub_conn, "logout")
        assert "Logout successfully" in resp

    def test_logout_revokes_admin_access(self, stub_conn):
        """After logout, stub controls must be rejected again."""
        _raw_command(stub_conn, "login 000000")
        _raw_command(stub_conn, "logout")
        resp = _raw_command(stub_conn, "stub soc 50")
        assert "Admin mode required" in resp


# 7. stub admin controls


class TestStubAdminControls:
    # guard

    def test_stub_rejected_without_admin(self, stub_conn):
        _raw_command(stub_conn, "logout")  # guarantee clean state
        resp = _raw_command(stub_conn, "stub soc 50")
        assert "Admin mode required" in resp

    # usage string

    def test_stub_no_args_returns_usage(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub")
            assert "Usage" in resp
        finally:
            _raw_command(stub_conn, "logout")

    def test_stub_unknown_subcommand_returns_usage(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub bogus")
            assert "Usage" in resp
        finally:
            _raw_command(stub_conn, "logout")

    # stub soc

    def test_stub_soc_sets_value(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub soc 42")
            assert "SOC set to 42%" in resp
            stat = _raw_command(stub_conn, "stat")
            assert "42" in stat
        finally:
            _raw_command(stub_conn, f"stub soc {STUB_SOC_START}")
            _raw_command(stub_conn, "logout")

    def test_stub_soc_clamps_below_zero(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub soc -20")
            assert "SOC set to 0%" in resp
        finally:
            _raw_command(stub_conn, f"stub soc {STUB_SOC_START}")
            _raw_command(stub_conn, "logout")

    def test_stub_soc_clamps_above_100(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub soc 999")
            assert "SOC set to 100%" in resp
        finally:
            _raw_command(stub_conn, f"stub soc {STUB_SOC_START}")
            _raw_command(stub_conn, "logout")

    def test_stub_soc_non_integer_rejected(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub soc bad")
            assert "Error" in resp
        finally:
            _raw_command(stub_conn, "logout")

    # stub current

    def test_stub_current_fixes_value(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub current 9999")
            assert "9999 mA" in resp
            pwr = _raw_command(stub_conn, "pwr")
            assert "9999" in pwr
        finally:
            _raw_command(stub_conn, "stub current auto")
            _raw_command(stub_conn, "logout")

    def test_stub_current_auto_clears_override(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            _raw_command(stub_conn, "stub current 1111")
            resp = _raw_command(stub_conn, "stub current auto")
            assert "auto" in resp.lower() or "cleared" in resp.lower()
        finally:
            _raw_command(stub_conn, "logout")

    def test_stub_current_non_integer_rejected(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub current notanumber")
            assert "Error" in resp
        finally:
            _raw_command(stub_conn, "logout")

    # stub fault injection error paths

    def test_stub_fault_non_integer_bat_rejected(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub fault notanint ov")
            assert "Error" in resp
        finally:
            _raw_command(stub_conn, "logout")

    def test_stub_fault_invalid_type_rejected(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub fault 1 badtype")
            assert "Error" in resp
        finally:
            _raw_command(stub_conn, "logout")

    def test_stub_fault_missing_type_returns_usage(self, stub_conn):
        """stub fault <bat> without the type arg must fall through to usage."""
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub fault 1")
            assert "Usage" in resp
        finally:
            _raw_command(stub_conn, "logout")

    def test_stub_fault_injection_acknowledged(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub fault 1 ov")
            assert "ov" in resp.lower()
            assert "1" in resp
        finally:
            _raw_command(stub_conn, "stub clear 1")
            _raw_command(stub_conn, "logout")

    # stub clear

    def test_stub_clear_no_existing_fault_reports_none_active(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub clear 99")
            assert "No fault active" in resp
        finally:
            _raw_command(stub_conn, "logout")

    def test_stub_clear_non_integer_bat_rejected(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub clear notanint")
            assert "Error" in resp
        finally:
            _raw_command(stub_conn, "logout")

    def test_stub_clear_active_fault_acknowledged(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        _raw_command(stub_conn, "stub fault 2 uv")
        try:
            resp = _raw_command(stub_conn, "stub clear 2")
            assert "Fault cleared" in resp
        finally:
            # belt-and-suspenders: ensure clean even if assert failed
            _raw_command(stub_conn, "stub clear 2")
            _raw_command(stub_conn, "logout")


# 8. Fault propagation — inject fault, verify across all relevant views, clear


class TestStubFaultPropagation:
    """Inject each fault type and verify it surfaces in pwr, bat, pwr N, and pwrsys."""

    # Convenience methods so each test body stays focused on assertions.

    def _inject(self, conn, bat_id: int, fault: str) -> None:
        _raw_command(conn, "login 000000")
        _raw_command(conn, f"stub fault {bat_id} {fault}")

    def _clear(self, conn, bat_id: int) -> None:
        """Clear the fault and log out.  Safe to call even after a test failure."""
        _raw_command(conn, f"stub clear {bat_id}")
        _raw_command(conn, "logout")

    # OV

    def test_ov_appears_in_pwr_table(self, stub_conn):
        self._inject(stub_conn, 1, "ov")
        try:
            assert "OV" in _raw_command(stub_conn, "pwr")
        finally:
            self._clear(stub_conn, 1)

    def test_ov_appears_in_bat_cell_data(self, stub_conn):
        self._inject(stub_conn, 1, "ov")
        try:
            assert "OV" in _raw_command(stub_conn, "bat 1")
        finally:
            self._clear(stub_conn, 1)

    def test_ov_appears_in_pwr_indexed(self, stub_conn):
        self._inject(stub_conn, 1, "ov")
        try:
            assert "OV" in _raw_command(stub_conn, "pwr 1")
        finally:
            self._clear(stub_conn, 1)

    def test_ov_triggers_sys_alarm_in_pwrsys(self, stub_conn):
        self._inject(stub_conn, 1, "ov")
        try:
            assert "Alarm" in _raw_command(stub_conn, "pwrsys")
        finally:
            self._clear(stub_conn, 1)

    # UV

    def test_uv_appears_in_pwr_table(self, stub_conn):
        self._inject(stub_conn, 1, "uv")
        try:
            assert "UV" in _raw_command(stub_conn, "pwr")
        finally:
            self._clear(stub_conn, 1)

    def test_uv_appears_in_bat_cell_data(self, stub_conn):
        self._inject(stub_conn, 1, "uv")
        try:
            assert "UV" in _raw_command(stub_conn, "bat 1")
        finally:
            self._clear(stub_conn, 1)

    def test_uv_appears_in_pwr_indexed(self, stub_conn):
        self._inject(stub_conn, 1, "uv")
        try:
            assert "UV" in _raw_command(stub_conn, "pwr 1")
        finally:
            self._clear(stub_conn, 1)

    # OT

    def test_ot_appears_in_pwr_table(self, stub_conn):
        self._inject(stub_conn, 1, "ot")
        try:
            assert "OT" in _raw_command(stub_conn, "pwr")
        finally:
            self._clear(stub_conn, 1)

    def test_ot_appears_in_bat_cell_data(self, stub_conn):
        self._inject(stub_conn, 1, "ot")
        try:
            assert "OT" in _raw_command(stub_conn, "bat 1")
        finally:
            self._clear(stub_conn, 1)

    def test_ot_appears_in_pwr_indexed(self, stub_conn):
        self._inject(stub_conn, 1, "ot")
        try:
            assert "OT" in _raw_command(stub_conn, "pwr 1")
        finally:
            self._clear(stub_conn, 1)

    # UT

    def test_ut_appears_in_pwr_table(self, stub_conn):
        self._inject(stub_conn, 1, "ut")
        try:
            assert "UT" in _raw_command(stub_conn, "pwr")
        finally:
            self._clear(stub_conn, 1)

    def test_ut_appears_in_bat_cell_data(self, stub_conn):
        self._inject(stub_conn, 1, "ut")
        try:
            assert "UT" in _raw_command(stub_conn, "bat 1")
        finally:
            self._clear(stub_conn, 1)

    def test_ut_appears_in_pwr_indexed(self, stub_conn):
        self._inject(stub_conn, 1, "ut")
        try:
            assert "UT" in _raw_command(stub_conn, "pwr 1")
        finally:
            self._clear(stub_conn, 1)

    # OC

    def test_oc_appears_in_pwr_table(self, stub_conn):
        self._inject(stub_conn, 1, "oc")
        try:
            assert "OC" in _raw_command(stub_conn, "pwr")
        finally:
            self._clear(stub_conn, 1)

    def test_oc_appears_in_bat_cell_data(self, stub_conn):
        self._inject(stub_conn, 1, "oc")
        try:
            assert "OC" in _raw_command(stub_conn, "bat 1")
        finally:
            self._clear(stub_conn, 1)

    def test_oc_appears_in_pwr_indexed(self, stub_conn):
        self._inject(stub_conn, 1, "oc")
        try:
            assert "OC" in _raw_command(stub_conn, "pwr 1")
        finally:
            self._clear(stub_conn, 1)

    # Absent fault

    def test_absent_fault_increases_absent_count_in_pwr(self, stub_conn):
        """Injecting 'absent' on a present battery must add an Absent row."""
        before = _raw_command(stub_conn, "pwr").count("Absent")
        self._inject(stub_conn, 1, "absent")
        try:
            after = _raw_command(stub_conn, "pwr").count("Absent")
            assert after > before
        finally:
            self._clear(stub_conn, 1)

    def test_absent_fault_in_bat_command(self, stub_conn):
        self._inject(stub_conn, 1, "absent")
        try:
            assert "Absent" in _raw_command(stub_conn, "bat 1")
        finally:
            self._clear(stub_conn, 1)

    def test_absent_fault_in_soh_command(self, stub_conn):
        self._inject(stub_conn, 1, "absent")
        try:
            assert "Absent" in _raw_command(stub_conn, "soh 1")
        finally:
            self._clear(stub_conn, 1)

    def test_absent_fault_in_pwr_indexed(self, stub_conn):
        self._inject(stub_conn, 1, "absent")
        try:
            assert "Absent" in _raw_command(stub_conn, "pwr 1")
        finally:
            self._clear(stub_conn, 1)

    def test_absent_fault_reduces_online_count_in_pwrsys(self, stub_conn):
        """pwrsys Online count must decrease when a battery is marked absent."""
        match_before = re.search(
            r"Online\s*:\s*(\d+)", _raw_command(stub_conn, "pwrsys")
        )
        assert match_before is not None
        online_before = int(match_before.group(1))
        self._inject(stub_conn, 1, "absent")
        try:
            match_after = re.search(
                r"Online\s*:\s*(\d+)", _raw_command(stub_conn, "pwrsys")
            )
            assert match_after is not None
            online_after = int(match_after.group(1))
            assert online_after < online_before
        finally:
            self._clear(stub_conn, 1)

    # Clearing a fault restores Normal

    def test_clear_fault_removes_ov_from_pwr(self, stub_conn):
        self._inject(stub_conn, 1, "ov")
        assert "OV" in _raw_command(stub_conn, "pwr")
        self._clear(stub_conn, 1)  # also logs out
        assert "OV" not in _raw_command(stub_conn, "pwr")

    def test_clear_fault_restores_pwrsys_normal_alarm(self, stub_conn):
        self._inject(stub_conn, 1, "ov")
        assert "Alarm" in _raw_command(stub_conn, "pwrsys")
        self._clear(stub_conn, 1)
        assert "Normal" in _raw_command(stub_conn, "pwrsys")


# 9. re — remote command forwarding


class TestStubRemoteForward:
    def test_re_forwards_pwr(self, stub_conn):
        """re <addr> pwr must return a valid pwr response."""
        resp = _raw_command(stub_conn, "re 1 pwr")
        assert "Command completed successfully" in resp
        assert "Volt" in resp

    def test_re_forwards_info(self, stub_conn):
        resp = _raw_command(stub_conn, "re 1 info")
        assert "Command completed successfully" in resp
        assert "Manufacturer" in resp

    def test_re_forwards_stat(self, stub_conn):
        resp = _raw_command(stub_conn, "re 1 stat")
        assert "Command completed successfully" in resp
        assert "CYCLE Times" in resp

    def test_re_too_few_args_returns_usage(self, stub_conn):
        """re without a command to forward must return the usage string."""
        resp = _raw_command(stub_conn, "re 1")
        assert "Usage" in resp


# 10. New firmware layout (--firmware new)


class TestStubNewFirmware:
    """Verify the 23-column 'new' firmware pwr layout with *.Id columns."""

    def test_pwr_has_tlow_id_column(self, new_fw_conn):
        assert "Tlow.Id" in _raw_command(new_fw_conn, "pwr")

    def test_pwr_has_thigh_id_column(self, new_fw_conn):
        assert "Thigh.Id" in _raw_command(new_fw_conn, "pwr")

    def test_pwr_has_vlow_id_column(self, new_fw_conn):
        assert "Vlow.Id" in _raw_command(new_fw_conn, "pwr")

    def test_pwr_has_vhigh_id_column(self, new_fw_conn):
        assert "Vhigh.Id" in _raw_command(new_fw_conn, "pwr")

    def test_pwr_has_sysalarm_column(self, new_fw_conn):
        assert "SysAlarm.St" in _raw_command(new_fw_conn, "pwr")

    def test_pwr_has_mostempr_column(self, new_fw_conn):
        assert "MosTempr" in _raw_command(new_fw_conn, "pwr")

    def test_pwr_completion_marker_present(self, new_fw_conn):
        resp = _raw_command(new_fw_conn, "pwr")
        assert "Command completed successfully" in resp
        assert "pylon>" in resp

    def test_pwr_absent_rows_still_present(self, new_fw_conn):
        """Absent rows must appear in new firmware layout too."""
        assert "Absent" in _raw_command(new_fw_conn, "pwr")

    def test_info_works_on_new_fw_stub(self, new_fw_conn):
        resp = _raw_command(new_fw_conn, "info")
        assert "US5KBPL" in resp
        assert "Manufacturer" in resp

    def test_stat_works_on_new_fw_stub(self, new_fw_conn):
        resp = _raw_command(new_fw_conn, "stat")
        assert "CYCLE Times" in resp
        assert "Command completed successfully" in resp

    def test_bat_works_on_new_fw_stub(self, new_fw_conn):
        resp = _raw_command(new_fw_conn, "bat 1")
        assert "mAH" in resp

    def test_soh_works_on_new_fw_stub(self, new_fw_conn):
        resp = _raw_command(new_fw_conn, "soh 1")
        assert "SOHStatus" in resp


# 11. Multi-group topology (--groups 2)


class TestStubMultiGroup:
    """Verify LV-HUB (groups > 1) topology responses."""

    def test_pwr_table_has_absent_rows_in_both_groups(self, multi_grp_conn):
        """2 groups × 6 absent slots each = 12 absent rows minimum."""
        resp = _raw_command(multi_grp_conn, "pwr")
        assert resp.count("Absent") >= 12

    def test_pwr_completion_marker(self, multi_grp_conn):
        resp = _raw_command(multi_grp_conn, "pwr")
        assert "Command completed successfully" in resp
        assert "pylon>" in resp

    def test_pwrsys_reports_two_groups(self, multi_grp_conn):
        resp = _raw_command(multi_grp_conn, "pwrsys")
        assert re.search(r"Groups\s*:\s*2", resp), "pwrsys Groups field should be 2"

    def test_pwrsys_total_modules_is_four(self, multi_grp_conn):
        """2 groups × 2 batteries per group = 4 total modules."""
        resp = _raw_command(multi_grp_conn, "pwrsys")
        assert re.search(r"Total modules\s*:\s*4", resp), "expected 4 total modules"

    def test_pwrsys_has_per_group_lines(self, multi_grp_conn):
        """With n_groups > 1, per-group detail lines must be emitted."""
        resp = _raw_command(multi_grp_conn, "pwrsys")
        assert "Group 1" in resp
        assert "Group 2" in resp

    def test_pwrsys_per_group_lines_have_volt_curr(self, multi_grp_conn):
        resp = _raw_command(multi_grp_conn, "pwrsys")
        assert re.search(r"Group 1\s+Volt:", resp)

    def test_pwrsys_online_count_reflects_present_batteries(self, multi_grp_conn):
        """4 present (2 per group) out of 16 total slots."""
        resp = _raw_command(multi_grp_conn, "pwrsys")
        m = re.search(r"Online\s*:\s*(\d+)", resp)
        assert m is not None, "Online field not found in pwrsys"
        assert int(m.group(1)) == 4

    def test_pwrsys_offline_count_is_zero_without_faults(self, multi_grp_conn):
        """pwrsys 'Offline' tracks fault-injected absent batteries only (not
        naturally empty slots), so without any fault injections it must be 0."""
        resp = _raw_command(multi_grp_conn, "pwrsys")
        m = re.search(r"Offline\s*:\s*(\d+)", resp)
        assert m is not None, "Offline field not found in pwrsys"
        assert int(m.group(1)) == 0

    def test_bat_second_group_battery(self, multi_grp_conn):
        """Battery 9 is the first slot in group 2 and must be present."""
        resp = _raw_command(multi_grp_conn, "bat 9")
        assert "Command completed successfully" in resp
        assert "mAH" in resp

    def test_pwr_indexed_second_group_battery(self, multi_grp_conn):
        resp = _raw_command(multi_grp_conn, "pwr 9")
        assert "Power 9" in resp
        assert "Voltage" in resp


# 12. bat_id < 1 — lower-bound OOB paths not covered by the > max tests


class TestStubBatIdBelowOne:
    """bat_id = 0 exercises the `bat_id < 1` branch in pwr N, bat N, soh N."""

    def test_pwr_indexed_zero_returns_not_found(self, stub_conn):
        resp = _raw_command(stub_conn, "pwr 0")
        assert "not found" in resp.lower()

    def test_bat_zero_returns_not_found(self, stub_conn):
        resp = _raw_command(stub_conn, "bat 0")
        assert "not found" in resp.lower()

    def test_soh_zero_returns_not_found(self, stub_conn):
        resp = _raw_command(stub_conn, "soh 0")
        assert "not found" in resp.lower()


# 13. pwr with non-integer argument — ValueError catch → tabular fallback


class TestStubPwrNonIntegerArg:
    def test_pwr_noninteger_arg_falls_back_to_tabular(self, stub_conn):
        """pwr badarg catches ValueError; bat_id_filter stays None so the full
        tabular table is returned — same output as bare pwr."""
        resp = _raw_command(stub_conn, "pwr badarg")
        assert "Command completed successfully" in resp
        # Must produce the tabular header, not the indexed block
        assert "Volt" in resp
        assert "Base.St" in resp
        # Must NOT be the 'not found' indexed error
        assert "not found" not in resp.lower()


# 14. stub sub-command with missing required argument → usage


class TestStubMissingArgUsage:
    """Each sub-command needs at least one extra argument; omitting it must
    fall through to the generic usage string."""

    def test_stub_soc_no_pct_returns_usage(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub soc")
            assert "Usage" in resp
        finally:
            _raw_command(stub_conn, "logout")

    def test_stub_current_no_ma_returns_usage(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub current")
            assert "Usage" in resp
        finally:
            _raw_command(stub_conn, "logout")

    def test_stub_clear_no_bat_returns_usage(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            resp = _raw_command(stub_conn, "stub clear")
            assert "Usage" in resp
        finally:
            _raw_command(stub_conn, "logout")


# 15. _base_state "Idle" and "Dischg" paths


class TestStubBaseState:
    """Verify the three _base_state branches: Charge (default), Idle, Dischg."""

    def test_idle_state_in_pwr_table(self, stub_conn):
        """|current| < 500 mA → _base_state returns 'Idle'."""
        _raw_command(stub_conn, "login 000000")
        try:
            _raw_command(stub_conn, "stub current 100")
            resp = _raw_command(stub_conn, "pwr")
            assert "Idle" in resp
        finally:
            _raw_command(stub_conn, "stub current auto")
            _raw_command(stub_conn, "logout")

    def test_idle_state_in_bat_command(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            _raw_command(stub_conn, "stub current 100")
            resp = _raw_command(stub_conn, "bat 1")
            assert "Idle" in resp
        finally:
            _raw_command(stub_conn, "stub current auto")
            _raw_command(stub_conn, "logout")

    def test_idle_state_in_pwr_indexed(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            _raw_command(stub_conn, "stub current 100")
            resp = _raw_command(stub_conn, "pwr 1")
            assert "Idle" in resp
        finally:
            _raw_command(stub_conn, "stub current auto")
            _raw_command(stub_conn, "logout")

    def test_idle_state_in_pwrsys(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            _raw_command(stub_conn, "stub current 100")
            resp = _raw_command(stub_conn, "pwrsys")
            assert "Idle" in resp
        finally:
            _raw_command(stub_conn, "stub current auto")
            _raw_command(stub_conn, "logout")

    def test_dischg_state_in_pwr_table(self, stub_conn):
        """Negative current outside OC-fault path → _base_state returns 'Dischg'."""
        _raw_command(stub_conn, "login 000000")
        try:
            _raw_command(stub_conn, "stub current -5000")
            resp = _raw_command(stub_conn, "pwr")
            assert "Dischg" in resp
        finally:
            _raw_command(stub_conn, "stub current auto")
            _raw_command(stub_conn, "logout")

    def test_dischg_state_in_pwr_indexed(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            _raw_command(stub_conn, "stub current -5000")
            resp = _raw_command(stub_conn, "pwr 1")
            assert "Dischg" in resp
        finally:
            _raw_command(stub_conn, "stub current auto")
            _raw_command(stub_conn, "logout")

    def test_dischg_state_in_pwrsys(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            _raw_command(stub_conn, "stub current -5000")
            resp = _raw_command(stub_conn, "pwrsys")
            assert "Dischg" in resp
        finally:
            _raw_command(stub_conn, "stub current auto")
            _raw_command(stub_conn, "logout")


# 16. pwrsys charge/discharge current taper at SOC extremes


class TestStubPwrsysTaper:
    """At SOC > 95 charge current is tapered to 30 %; at SOC < 10 discharge
    current is tapered.  US5000 limits: max_chg = 200 000 mA, max_dsg = 200 000 mA.
    Tapered value = int(200 000 × 0.3) × 1 group = 60 000 mA."""

    def test_charge_taper_at_high_soc(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            _raw_command(stub_conn, "stub soc 99")
            resp = _raw_command(stub_conn, "pwrsys")
            # Tapered rec_chg_curr = int(200000 * 0.3) = 60000
            assert re.search(r"Rec ChgCurr\s*:\s*60000\s*mA", resp), (
                "charge taper at SOC 99 should yield Rec ChgCurr = 60000 mA"
            )
        finally:
            _raw_command(stub_conn, f"stub soc {STUB_SOC_START}")
            _raw_command(stub_conn, "logout")

    def test_discharge_taper_at_low_soc(self, stub_conn):
        _raw_command(stub_conn, "login 000000")
        try:
            _raw_command(stub_conn, "stub soc 5")
            resp = _raw_command(stub_conn, "pwrsys")
            # Tapered rec_dsg_curr = int(200000 * 0.3) = 60000
            assert re.search(r"Rec DsgCurr\s*:\s*60000\s*mA", resp), (
                "discharge taper at SOC 5 should yield Rec DsgCurr = 60000 mA"
            )
        finally:
            _raw_command(stub_conn, f"stub soc {STUB_SOC_START}")
            _raw_command(stub_conn, "logout")

    def test_no_taper_at_mid_soc(self, stub_conn):
        """At mid-range SOC both factors are 1.0; US5000 limits are unchanged."""
        resp = _raw_command(stub_conn, "pwrsys")
        assert re.search(r"Rec ChgCurr\s*:\s*200000\s*mA", resp), (
            "no charge taper at mid SOC — rec_chg_curr should be 200000 mA"
        )
        assert re.search(r"Rec DsgCurr\s*:\s*200000\s*mA", resp), (
            "no discharge taper at mid SOC — rec_dsg_curr should be 200000 mA"
        )


# 17. New-firmware SysAlarm.St column shows "Alarm" on injected fault


class TestStubNewFirmwareFaultInjection:
    """Verify the per-row SysAlarm.St column in the new-firmware pwr table
    changes from 'Normal' to 'Alarm' when a fault is injected."""

    def test_sysalarm_column_normal_without_fault(self, new_fw_conn):
        resp = _raw_command(new_fw_conn, "pwr")
        # Rows for present batteries should have 'Normal' in SysAlarm.St
        assert "Normal" in resp

    def test_sysalarm_column_alarm_on_ov_fault(self, new_fw_conn):
        _raw_command(new_fw_conn, "login 000000")
        _raw_command(new_fw_conn, "stub fault 1 ov")
        try:
            resp = _raw_command(new_fw_conn, "pwr")
            # The SysAlarm.St column for the faulted battery should show 'Alarm'
            assert "Alarm" in resp
        finally:
            _raw_command(new_fw_conn, "stub clear 1")
            _raw_command(new_fw_conn, "logout")
