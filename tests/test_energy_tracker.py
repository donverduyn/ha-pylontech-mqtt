"""Unit tests for EnergyTracker (docker/main.py)."""

import json
import os
from unittest.mock import patch

import pytest
from main import EnergyTracker


class TestEnergyTrackerFirstCall:
    def test_first_call_does_not_accumulate_energy(self):
        """The first update() has no previous sample so nothing is counted."""
        tracker = EnergyTracker()
        with patch("main.time.monotonic", side_effect=[0.0]):
            tracker.update(1000.0)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0


class TestEnergyTrackerCharging:
    def test_positive_power_adds_to_energy_in(self):
        """1000 W over 1 h = 1.0 kWh in."""
        tracker = EnergyTracker()
        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 3600.0]):
            tracker.update(1000.0)
            tracker.update(1000.0)
        assert tracker.energy_in == pytest.approx(1.0)
        assert tracker.energy_out == 0.0

    def test_zero_power_counts_as_charging(self):
        """power == 0 satisfies `power >= 0` → goes to energy_in (adds 0 kWh)."""
        tracker = EnergyTracker()
        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 3600.0]):
            tracker.update(0.0)
            tracker.update(0.0)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0


class TestEnergyTrackerDischarging:
    def test_negative_power_adds_to_energy_out(self):
        """−1000 W over 1 h = 1.0 kWh out."""
        tracker = EnergyTracker()
        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 3600.0]):
            tracker.update(-1000.0)
            tracker.update(-1000.0)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == pytest.approx(1.0)


class TestEnergyTrackerCumulative:
    def test_multiple_charge_intervals(self):
        """Four calls → three 30-min intervals at a constant 2000 W = 3.0 kWh in."""
        tracker = EnergyTracker()
        times = [0.0, 1800.0, 3600.0, 5400.0]
        with patch("main.time.monotonic", side_effect=times):
            for _ in times:
                tracker.update(2000.0)
        assert tracker.energy_in == pytest.approx(3.0)


class TestEnergyTrackerTrapezoidalIntegration:
    """EnergyTracker averages the two endpoint power samples of each interval
    (trapezoidal) instead of assuming the whole interval was at the latest
    reading (rectangular/step) — see docker/main.py EnergyTracker.update()."""

    def test_ramp_uses_average_of_endpoints_not_latest_sample(self):
        """1000 W → 2000 W over 1 h must integrate to 1.5 kWh, not 2.0 kWh.

        Rectangular integration would attribute the entire hour to the
        latest 2000 W reading (2.0 kWh); trapezoidal integration averages
        the two endpoints (1500 W) for a more accurate 1.5 kWh.
        """
        tracker = EnergyTracker()
        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 3600.0]):
            tracker.update(1000.0)
            tracker.update(2000.0)
        assert tracker.energy_in == pytest.approx(1.5)
        assert tracker.energy_out == 0.0

    def test_sign_crossing_splits_at_interpolated_zero_crossing(self):
        """A charge→discharge crossing spanning exactly one interval must
        split at the interpolated zero-crossing, not net to ~0.

        With only two endpoint samples (+500 W then −500 W), integration
        assumes power varied linearly between them — which crosses zero at
        the interval's midpoint here — so half the hour (500 W → 0 W)
        integrates to 0.125 kWh in and the other half (0 W → −500 W) to
        0.125 kWh out. Averaging the endpoints into a single 0 W figure
        would silently drop that real throughput from both counters.
        """
        tracker = EnergyTracker()
        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 7200.0]):
            tracker.update(500.0)  # first call: no accumulation
            tracker.update(500.0)  # 1 h at a constant 500 W → 0.5 kWh in
            tracker.update(-500.0)  # +500 W -> -500 W crosses zero at 30 min
        assert tracker.energy_in == pytest.approx(0.5 + 0.125)
        assert tracker.energy_out == pytest.approx(0.125)

    def test_crossing_closer_to_one_endpoint_splits_unevenly(self):
        """The zero-crossing point must track where a linear interpolation
        actually reaches 0, not always assume the interval's midpoint.

        +1000 W -> -500 W over 1 h crosses zero at 2/3 of the interval
        (t* = 1000 / (1000 - (-500)) = 2/3 h). First segment: 1000 W -> 0 W
        over 2/3 h averages 500 W = 1/3 kWh in. Second segment: 0 W ->
        -500 W over 1/3 h averages -250 W = 1/12 kWh out.
        """
        tracker = EnergyTracker()
        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 3600.0]):
            tracker.update(1000.0)
            tracker.update(-500.0)
        assert tracker.energy_in == pytest.approx(1.0 / 3.0)
        assert tracker.energy_out == pytest.approx(1.0 / 12.0)


class TestEnergyTrackerSanityLimits:
    def test_negative_elapsed_time_is_ignored(self):
        """A monotonic clock can't go backwards, but the guard must hold even
        if it somehow did — no energy should be counted from it."""
        tracker = EnergyTracker()
        with patch("main.time.monotonic", side_effect=[100.0, 50.0]):
            tracker.update(1000.0)
            tracker.update(1000.0)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0

    def test_unreasonably_long_gap_is_skipped(self, caplog):
        """A gap longer than _MAX_INTERVAL_SECONDS (e.g. a missed
        invalidate_last_time() call, or a clock jump) must not be integrated
        as if it were a real multi-hour reading."""
        import logging

        tracker = EnergyTracker()
        with (
            patch("main.time.monotonic", side_effect=[0.0, 7200.0]),
            caplog.at_level(logging.WARNING),
        ):
            tracker.update(1000.0)
            tracker.update(1000.0)  # 2 h gap > _MAX_INTERVAL_SECONDS (1 h)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0
        assert "Skipping energy integration" in caplog.text

    def test_gap_at_the_limit_is_still_integrated(self):
        """A gap exactly at _MAX_INTERVAL_SECONDS must still be counted —
        only gaps *longer* than the limit are treated as anomalous."""
        tracker = EnergyTracker()
        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 3600.0]):
            tracker.update(1000.0)
            tracker.update(1000.0)
        assert tracker.energy_in == pytest.approx(1.0)


class TestEnergyTrackerInvalidate:
    def test_invalidate_prevents_phantom_energy_after_gap(self):
        """After a comms gap, invalidate_last_time() must prevent energy being
        attributed to the outage period.

        Without the fix, reconnecting after a 30-minute outage with 2000 W
        would falsely add 2000*0.5/1000 = 1.0 kWh.
        """
        tracker = EnergyTracker()
        with patch("main.time.monotonic", side_effect=[0.0, 1800.0]):
            tracker.update(2000.0)  # establish _last_time / _last_power
            tracker.invalidate_last_time()  # simulate reconnect — drop the gap
            tracker.update(2000.0)  # must NOT count the 30-min gap

        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0

    def test_invalidate_does_not_reset_accumulated_totals(self):
        """Calling invalidate_last_time() must not zero energy_in / energy_out."""
        tracker = EnergyTracker()
        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 3600.0]):
            tracker.update(1000.0)  # set _last_time
            tracker.update(1000.0)  # 1 h at 1000 W → 1.0 kWh

        tracker.invalidate_last_time()  # drop timestamp; totals must survive

        assert tracker.energy_in == pytest.approx(1.0)
        assert tracker.energy_out == 0.0


# Persistence — state_file save / load


class TestEnergyTrackerPersistence:
    def test_no_state_file_starts_at_zero(self):
        """EnergyTracker() without a state_file must start at 0 (no side-effects)."""
        tracker = EnergyTracker()
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0

    def test_counters_restored_after_restart(self, tmp_path):
        """Energy accumulated in one instance must be visible in a second instance
        that reads the same state file — simulating a container restart."""
        state_file = str(tmp_path / "energy.json")

        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 3600.0]):
            tracker = EnergyTracker(state_file=state_file)
            tracker.update(1000.0)  # first call: no accumulation
            tracker.update(1000.0)  # 1 h at 1000 W → 1.0 kWh in

        # Simulate restart: new instance reads the same file
        tracker2 = EnergyTracker(state_file=state_file)
        assert tracker2.energy_in == pytest.approx(1.0)
        assert tracker2.energy_out == 0.0

    def test_discharge_energy_persisted(self, tmp_path):
        state_file = str(tmp_path / "energy.json")

        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 3600.0]):
            tracker = EnergyTracker(state_file=state_file)
            tracker.update(-500.0)
            tracker.update(-500.0)  # 1 h at -500 W → 0.5 kWh out

        tracker2 = EnergyTracker(state_file=state_file)
        assert tracker2.energy_in == 0.0
        assert tracker2.energy_out == pytest.approx(0.5)

    def test_missing_state_file_starts_at_zero(self, tmp_path):
        """A missing state file must silently start counters at 0."""
        state_file = str(tmp_path / "nonexistent.json")
        tracker = EnergyTracker(state_file=state_file)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0

    def test_corrupt_state_file_starts_at_zero(self, tmp_path):
        """A corrupt JSON file must be silently ignored; counters start at 0."""
        state_file = str(tmp_path / "energy.json")
        with open(state_file, "w") as f:
            f.write("not valid json{{")

        tracker = EnergyTracker(state_file=state_file)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0

    def test_partial_state_file_starts_at_zero(self, tmp_path):
        """A JSON file missing expected keys must be silently ignored."""
        state_file = str(tmp_path / "energy.json")
        with open(state_file, "w") as f:
            json.dump({"energy_in": 1.5}, f)  # missing energy_out

        tracker = EnergyTracker(state_file=state_file)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0

    def test_invalidate_does_not_clear_persisted_state(self, tmp_path):
        """invalidate_last_time() must not touch the state file."""
        state_file = str(tmp_path / "energy.json")

        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 3600.0]):
            tracker = EnergyTracker(state_file=state_file)
            tracker.update(1000.0)
            tracker.update(1000.0)  # 1 kWh in

        tracker.invalidate_last_time()

        tracker2 = EnergyTracker(state_file=state_file)
        assert tracker2.energy_in == pytest.approx(1.0)

    @pytest.mark.parametrize(
        "energy_in, energy_out",
        [
            ("nan", 1.0),
            (1.0, "nan"),
            ("inf", 1.0),
            ("-inf", 1.0),
            (-5.0, 1.0),
            (1.0, -5.0),
        ],
    )
    def test_non_finite_or_negative_persisted_values_reset_to_zero(
        self, tmp_path, caplog, energy_in, energy_out
    ):
        """A corrupt state file with NaN/inf/negative counters must not be
        loaded as-is — HA's coordinator rejects such values forever, so a
        poisoned file would otherwise permanently block all publishing
        instead of just losing the persisted counters."""
        import logging

        state_file = str(tmp_path / "energy.json")
        with open(state_file, "w") as f:
            json.dump({"energy_in": energy_in, "energy_out": energy_out}, f)

        with caplog.at_level(logging.WARNING):
            tracker = EnergyTracker(state_file=state_file)

        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0
        assert "Could not load energy state" in caplog.text

    def test_zero_persisted_values_are_accepted(self, tmp_path):
        """0.0 is a valid, finite, non-negative value and must load as-is."""
        state_file = str(tmp_path / "energy.json")
        with open(state_file, "w") as f:
            json.dump({"energy_in": 0.0, "energy_out": 0.0}, f)

        tracker = EnergyTracker(state_file=state_file)
        assert tracker.energy_in == 0.0
        assert tracker.energy_out == 0.0

    def test_save_is_atomic_no_stray_tmp_file_left_behind(self, tmp_path):
        """_save() writes to a sibling .tmp file and renames it into place —
        after a successful save, no leftover .tmp file should remain."""
        state_file = str(tmp_path / "energy.json")

        with patch("main.time.monotonic", side_effect=[0.0, 3600.0, 3600.0]):
            tracker = EnergyTracker(state_file=state_file)
            tracker.update(1000.0)
            tracker.update(1000.0)

        assert os.path.exists(state_file)
        assert not os.path.exists(f"{state_file}.tmp")
