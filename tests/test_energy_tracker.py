"""Unit tests for EnergyTracker (docker/main.py)."""

from datetime import datetime
from unittest.mock import patch

import pytest
from main import EnergyTracker


class TestEnergyTrackerInitialState:
    def test_energy_in_starts_at_zero(self):
        assert EnergyTracker().energy_in == 0.0

    def test_energy_out_starts_at_zero(self):
        assert EnergyTracker().energy_out == 0.0


class TestEnergyTrackerFirstCall:
    def test_first_call_does_not_accumulate_energy(self):
        """The first update() has no previous timestamp so nothing is counted."""
        tracker = EnergyTracker()
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 1, 1, 12, 0, 0)
            tracker.update(1000.0)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0


class TestEnergyTrackerCharging:
    def test_positive_power_adds_to_energy_in(self):
        """1000 W over 1 h = 1.0 kWh in."""
        tracker = EnergyTracker()
        t0 = datetime(2024, 1, 1, 12, 0, 0)
        t1 = datetime(2024, 1, 1, 13, 0, 0)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.side_effect = [t0, t1]
            tracker.update(1000.0)
            tracker.update(1000.0)
        assert tracker.energy_in == pytest.approx(1.0)
        assert tracker.energy_out == 0.0

    def test_zero_power_counts_as_charging(self):
        """power == 0 satisfies `power >= 0` → goes to energy_in (adds 0 kWh)."""
        tracker = EnergyTracker()
        t0 = datetime(2024, 1, 1, 12, 0, 0)
        t1 = datetime(2024, 1, 1, 13, 0, 0)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.side_effect = [t0, t1]
            tracker.update(0.0)
            tracker.update(0.0)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0


class TestEnergyTrackerDischarging:
    def test_negative_power_adds_to_energy_out(self):
        """−1000 W over 1 h = 1.0 kWh out."""
        tracker = EnergyTracker()
        t0 = datetime(2024, 1, 1, 12, 0, 0)
        t1 = datetime(2024, 1, 1, 13, 0, 0)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.side_effect = [t0, t1]
            tracker.update(-1000.0)
            tracker.update(-1000.0)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == pytest.approx(1.0)


class TestEnergyTrackerCumulative:
    def test_multiple_charge_intervals(self):
        """Four calls → three 30-min intervals at 2000 W = 3.0 kWh in."""
        tracker = EnergyTracker()
        times = [
            datetime(2024, 1, 1, 12, 0, 0),
            datetime(2024, 1, 1, 12, 30, 0),
            datetime(2024, 1, 1, 13, 0, 0),
            datetime(2024, 1, 1, 13, 30, 0),
        ]
        with patch("main.datetime") as mock_dt:
            mock_dt.now.side_effect = times
            for _ in times:
                tracker.update(2000.0)
        assert tracker.energy_in == pytest.approx(3.0)

    def test_mixed_charge_then_discharge(self):
        """1 h charging then 1 h discharging at the same power level."""
        tracker = EnergyTracker()
        t0 = datetime(2024, 1, 1, 12, 0, 0)
        t1 = datetime(2024, 1, 1, 13, 0, 0)
        t2 = datetime(2024, 1, 1, 14, 0, 0)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.side_effect = [t0, t1, t2]
            tracker.update(500.0)  # first call: no accumulation
            tracker.update(500.0)  # 1 h at 500 W → 0.5 kWh in
            tracker.update(-500.0)  # 1 h at -500 W → 0.5 kWh out
        assert tracker.energy_in == pytest.approx(0.5)
        assert tracker.energy_out == pytest.approx(0.5)
