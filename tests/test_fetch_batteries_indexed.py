"""Unit tests for main._fetch_batteries_indexed — the 'pwr N' fallback path
used when the aggregate 'pwr' response doesn't look valid.
"""

import pytest

import main
from structs import PylontechBattery, PylontechSystem


class _FakeBms:
    """Duck-types BmsConnection.send_command against canned 'pwr N' blocks."""

    def __init__(self, responses: dict):
        self._responses = responses
        self.calls: list[str] = []

    def send_command(self, cmd: str) -> str:
        self.calls.append(cmd)
        bat_id = int(cmd.split()[1])
        return self._responses.get(bat_id, f"Power {bat_id} not found")


def _pwr_block(
    bat_id: int, *, voltage=51200, current=3806, temp=17000, soc=75, status="Charge"
) -> str:
    return (
        f"Power {bat_id}\r\n"
        f"Voltage         : {voltage}   mV\r\n"
        f"Current         : {current}    mA\r\n"
        f"Temperature     : {temp}   mC\r\n"
        f"Coulomb         : {soc}      %\r\n"
        f"Basic Status    : {status}\r\n"
        f"Volt Status     : Normal\r\n"
        f"Current Status  : Normal\r\n"
        f"Tmpr. Status    : Normal\r\n"
        f"Coul. Status    : Normal\r\n"
        f"Bat Events      : 0x0\r\n"
        f"Power Events    : 0x0\r\n"
        f"System Fault    : 0x0\r\n"
    )


def _new_system(**overrides) -> PylontechSystem:
    system = PylontechSystem(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    for key, value in overrides.items():
        setattr(system, key, value)
    return system


def test_stops_at_not_found(monkeypatch):
    monkeypatch.setattr(main, "MAX_BATTERIES", 16)
    bms = _FakeBms({1: _pwr_block(1), 2: _pwr_block(2)})
    system = _new_system()

    main._fetch_batteries_indexed(bms, system)

    assert [b.sys_id for b in system.batteries] == [1, 2]
    assert bms.calls == ["pwr 1", "pwr 2", "pwr 3"]


def test_skips_absent_slots_and_continues(monkeypatch):
    """An Absent slot in the middle of the range must be skipped, not treated
    as the end of the stack."""
    monkeypatch.setattr(main, "MAX_BATTERIES", 16)
    bms = _FakeBms(
        {
            1: _pwr_block(1),
            2: "Power 2\r\nBasic Status    : Absent\r\n",
            3: _pwr_block(3),
        }
    )
    system = _new_system()

    main._fetch_batteries_indexed(bms, system)

    assert [b.sys_id for b in system.batteries] == [1, 3]


def test_respects_max_batteries_cap(monkeypatch):
    monkeypatch.setattr(main, "MAX_BATTERIES", 3)
    bms = _FakeBms({i: _pwr_block(i) for i in range(1, 10)})
    system = _new_system()

    main._fetch_batteries_indexed(bms, system)

    assert len(system.batteries) == 3
    assert bms.calls == ["pwr 1", "pwr 2", "pwr 3"]


def test_aggregates_system_metrics(monkeypatch):
    monkeypatch.setattr(main, "MAX_BATTERIES", 16)
    bms = _FakeBms(
        {
            1: _pwr_block(1, voltage=51200, current=3000, soc=80),
            2: _pwr_block(2, voltage=51000, current=-1000, soc=70),
        }
    )
    system = _new_system()

    main._fetch_batteries_indexed(bms, system)

    assert system.voltage == pytest.approx((51.2 + 51.0) / 2, rel=1e-3)
    assert system.current == pytest.approx(3.0 - 1.0, rel=1e-3)
    assert system.soc == pytest.approx((80 + 70) / 2, rel=1e-3)
    assert system.power == pytest.approx(
        sum(b.power for b in system.batteries), rel=1e-3
    )


def test_no_batteries_zeroes_metrics(monkeypatch):
    """A prior poll's stale non-zero totals must not survive an empty result."""
    monkeypatch.setattr(main, "MAX_BATTERIES", 2)
    bms = _FakeBms({})  # every probe returns "not found"
    system = _new_system(voltage=51.0, current=20.0, soc=80.0, power=1020.0)

    main._fetch_batteries_indexed(bms, system)

    assert system.batteries == []
    assert system.voltage == 0.0
    assert system.current == 0.0
    assert system.soc == 0.0
    assert system.power == 0.0


# main._enrich_batteries_indexed — MONITORING_LEVEL medium/high detail walk
def _battery(sys_id: int, **overrides) -> PylontechBattery:
    bat = PylontechBattery(
        sys_id=sys_id,
        voltage=51.2,
        current=3.806,
        temperature=17.0,
        soc=75,
        status="Charge",
        power=195.0,
        energy_stored=0.0,
    )
    for key, value in overrides.items():
        setattr(bat, key, value)
    return bat


def test_enrich_adds_event_and_status_fields():
    bms = _FakeBms({1: _pwr_block(1)})
    system = _new_system()
    system.batteries = [_battery(1)]

    main._enrich_batteries_indexed(bms, system)

    bat = system.batteries[0]
    assert bat.coul_status == "Normal"
    assert bat.bat_events == 0
    assert bat.power_events == 0
    assert bat.sys_fault == 0


def test_enrich_does_not_override_aggregate_core_fields():
    """The aggregate 'pwr' table stays authoritative for voltage/current/soc
    — enrichment only adds fields the aggregate table doesn't expose."""
    bms = _FakeBms({1: _pwr_block(1, voltage=99999, current=99999, soc=1)})
    system = _new_system()
    system.batteries = [_battery(1, voltage=51.2, current=3.806, soc=75)]

    main._enrich_batteries_indexed(bms, system)

    bat = system.batteries[0]
    assert bat.voltage == 51.2
    assert bat.current == 3.806
    assert bat.soc == 75


def test_enrich_skips_absent_battery():
    """If 'pwr N' reports the battery absent, existing fields are left
    untouched and no exception propagates."""
    bms = _FakeBms({1: "Power 1\r\nBasic Status    : Absent\r\n"})
    system = _new_system()
    system.batteries = [_battery(1)]

    main._enrich_batteries_indexed(bms, system)

    bat = system.batteries[0]
    assert bat.coul_status is None
    assert bat.bat_events is None


def test_enrich_handles_send_command_failure(caplog):
    """A transport error fetching one battery's indexed detail must be
    logged and skipped, not abort enrichment for the remaining batteries."""
    import logging

    class _FailingBms:
        def send_command(self, cmd: str) -> str:
            raise TimeoutError("no response")

    system = _new_system()
    system.batteries = [_battery(1), _battery(2)]

    with caplog.at_level(logging.WARNING):
        main._enrich_batteries_indexed(_FailingBms(), system)

    assert system.batteries[0].coul_status is None
    assert system.batteries[1].coul_status is None
    assert "Could not fetch indexed detail" in caplog.text
