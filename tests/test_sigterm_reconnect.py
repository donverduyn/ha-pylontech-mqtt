"""Regression test for src/main.py's reconnect-sleep SIGTERM bypass.

The `except (serial.SerialException, OSError, IOError)` branch used to call
`time.sleep(5)` unprotected. A KeyboardInterrupt (raised by the SIGTERM
handler) landing during that sleep is not caught by any of the sibling
`except` clauses on the same `try` — those only catch exceptions raised in
the `try` body, not ones raised while another `except` clause is already
running — so it used to propagate straight out of the poll loop, skipping
_clean_shutdown entirely (no "offline" publish, no MQTT disconnect, no
bms.close(), no energy.flush()).
"""

import signal
from unittest.mock import MagicMock

import pytest

import main


class _RaisingBmsConnection:
    """Fails every send_command, driving main()'s poll loop straight into
    the reconnect except-block on its first iteration."""

    def send_command(self, *_args, **_kwargs):
        raise OSError("simulated BMS failure")

    def close(self):
        pass


def test_sigterm_during_reconnect_sleep_still_cleans_up(monkeypatch) -> None:
    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
    monkeypatch.setattr(main, "TCP_HOST", "localhost")
    monkeypatch.setattr(main, "ENERGY_STATE_FILE", "")

    client = MagicMock()
    monkeypatch.setattr(main, "_build_mqtt_client", lambda: client)
    monkeypatch.setattr(main, "BmsConnection", _RaisingBmsConnection)

    def fake_sleep(seconds: float) -> None:
        if seconds == 5:
            # Simulates a SIGTERM landing exactly during the reconnect delay.
            raise KeyboardInterrupt
        # Any other sleep (e.g. MQTT connect retry backoff) shouldn't block the test.

    monkeypatch.setattr(main.time, "sleep", fake_sleep)

    original_sigterm_handler = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit) as exc_info:
            main.main()
    finally:
        signal.signal(signal.SIGTERM, original_sigterm_handler)

    assert exc_info.value.code == 0
    assert client.loop_stop.called
    assert client.disconnect.called
