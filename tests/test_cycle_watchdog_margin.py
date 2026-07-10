"""Unit tests for main._warn_if_cycle_too_slow (src/main.py).

POLL_INTERVAL is validated at startup against _MAX_POLL_INTERVAL, but that
only bounds the configured sleep — not how long a poll cycle's actual BMS
round trips and MQTT publish took. _warn_if_cycle_too_slow is the runtime
check that catches a cycle eating into the safety margin below the HA
integration's 300s staleness watchdog even though POLL_INTERVAL itself is
compliant.
"""

import logging

import main


def test_fast_cycle_does_not_warn(monkeypatch, caplog) -> None:
    monkeypatch.setattr(main, "POLL_INTERVAL", 15)

    with caplog.at_level(logging.WARNING, logger="pylon2mqtt"):
        main._warn_if_cycle_too_slow(1.0)

    assert "poll cycle took" not in caplog.text


def test_cycle_within_margin_does_not_warn(monkeypatch, caplog) -> None:
    monkeypatch.setattr(main, "POLL_INTERVAL", 15)

    with caplog.at_level(logging.WARNING, logger="pylon2mqtt"):
        # elapsed + POLL_INTERVAL == _MAX_POLL_INTERVAL exactly — not "over".
        main._warn_if_cycle_too_slow(main._MAX_POLL_INTERVAL - 15)

    assert "poll cycle took" not in caplog.text


def test_slow_cycle_warns(monkeypatch, caplog) -> None:
    """A cycle that, combined with POLL_INTERVAL, exceeds _MAX_POLL_INTERVAL
    (e.g. MONITORING_LEVEL=high walking many batteries/cells on slow
    hardware) must warn — this is the actual failure mode the fixed 300s
    watchdog can't see on its own."""
    monkeypatch.setattr(main, "POLL_INTERVAL", 15)

    with caplog.at_level(logging.WARNING, logger="pylon2mqtt"):
        main._warn_if_cycle_too_slow(main._MAX_POLL_INTERVAL)

    assert "poll cycle took" in caplog.text
    assert "300s staleness watchdog" in caplog.text
