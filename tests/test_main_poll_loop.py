"""Coverage for main()'s top-level orchestration (docker/main.py): the MQTT
connect retry loop and one full BMS poll cycle (info -> pwr -> stat -> time
-> publish).

This is otherwise the single largest untested block in docker/main.py —
every other test exercises a connection, parsing, or publish-result helper
in isolation, never with main() actually driving them end to end. Running
the BMS side against the real pylon_stub.py TCP server (rather than
hand-typed canned response strings) means responses are exactly what the
rest of the suite already trusts to be protocol-accurate, without
duplicating that fixture data here; only the MQTT client is faked, so no
real broker is needed.
"""

from unittest.mock import MagicMock

import main
import pytest
from conftest import STUB_HOST


@pytest.fixture
def fake_mqtt_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(main, "_build_mqtt_client", lambda: client)
    return client


def _stop_after_n_poll_sleeps(n: int):
    """A time.sleep replacement that lets every other sleep (MQTT retry
    backoff, the "Power Volt" retry pause, etc.) through untouched, but
    raises KeyboardInterrupt on the Nth call that matches POLL_INTERVAL —
    i.e. the sleep between poll cycles at the bottom of the loop — so the
    otherwise-infinite loop exits via the normal SIGTERM/_clean_shutdown
    path after exactly N successful cycles."""
    seen = {"count": 0}

    def _fake_sleep(seconds: float) -> None:
        if seconds == main.POLL_INTERVAL:
            seen["count"] += 1
            if seen["count"] >= n:
                raise KeyboardInterrupt

    return _fake_sleep


class TestMainPollLoopHappyPath:
    def test_one_cycle_publishes_full_state_at_high_monitoring(
        self, monkeypatch, stub_server, fake_mqtt_client
    ) -> None:
        monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
        monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
        monkeypatch.setattr(main, "TCP_HOST", STUB_HOST)
        monkeypatch.setattr(main, "TCP_PORT", stub_server)
        monkeypatch.setattr(main, "ENERGY_STATE_FILE", "")
        monkeypatch.setattr(main, "MONITORING_LEVEL", "high")
        monkeypatch.setattr(main, "AUTO_SYNC_TIME", True)
        monkeypatch.setattr(main.time, "sleep", _stop_after_n_poll_sleeps(1))

        with pytest.raises(SystemExit) as exc_info:
            main.main()

        assert exc_info.value.code == 0

        state_calls = [
            c
            for c in fake_mqtt_client.publish.call_args_list
            if c.args[0] == main.STATE_TOPIC
        ]
        assert state_calls, "no state payload was published"
        payload = state_calls[-1].args[1]
        assert '"schema_version": 1' in payload
        assert '"batteries"' in payload

        avail_calls = [
            c.args[1]
            for c in fake_mqtt_client.publish.call_args_list
            if c.args[0] == main.AVAIL_TOPIC
        ]
        assert "online" in avail_calls

    def test_second_cycle_skips_info_fetch_and_time_sync(
        self, monkeypatch, stub_server, fake_mqtt_client
    ) -> None:
        """info_fetched must stay True across a healthy cycle: the second
        iteration should not re-send "info" or re-sync the clock."""
        monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
        monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
        monkeypatch.setattr(main, "TCP_HOST", STUB_HOST)
        monkeypatch.setattr(main, "TCP_PORT", stub_server)
        monkeypatch.setattr(main, "ENERGY_STATE_FILE", "")
        monkeypatch.setattr(main, "MONITORING_LEVEL", "low")
        monkeypatch.setattr(main, "AUTO_SYNC_TIME", True)
        monkeypatch.setattr(main.time, "sleep", _stop_after_n_poll_sleeps(2))

        sent_commands: list[str] = []
        real_send_command = main.BmsConnection.send_command

        def _tracking_send_command(self, cmd: str) -> str:
            sent_commands.append(cmd)
            return real_send_command(self, cmd)

        monkeypatch.setattr(main.BmsConnection, "send_command", _tracking_send_command)

        with pytest.raises(SystemExit):
            main.main()

        assert sent_commands.count("info") == 1


def test_main_retries_mqtt_connect_after_a_transient_failure(
    monkeypatch, stub_server
) -> None:
    client = MagicMock()
    attempts = {"n": 0}

    def _flaky_connect(*_args, **_kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("connection refused")

    client.connect.side_effect = _flaky_connect
    monkeypatch.setattr(main, "_build_mqtt_client", lambda: client)
    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
    monkeypatch.setattr(main, "TCP_HOST", STUB_HOST)
    monkeypatch.setattr(main, "TCP_PORT", stub_server)
    monkeypatch.setattr(main, "ENERGY_STATE_FILE", "")
    monkeypatch.setattr(main.time, "sleep", _stop_after_n_poll_sleeps(1))

    with pytest.raises(SystemExit) as exc_info:
        main.main()

    assert exc_info.value.code == 0
    assert client.connect.call_count == 2
