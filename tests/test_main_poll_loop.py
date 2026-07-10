"""Coverage for main()'s top-level orchestration (src/main.py): the MQTT
connect retry loop and one full BMS poll cycle (info -> pwr -> stat -> time
-> publish).

This is otherwise the single largest untested block in src/main.py —
every other test exercises a connection, parsing, or publish-result helper
in isolation, never with main() actually driving them end to end. Running
the BMS side against the real pylon_stub.py TCP server (rather than
hand-typed canned response strings) means responses are exactly what the
rest of the suite already trusts to be protocol-accurate, without
duplicating that fixture data here; only the MQTT client is faked, so no
real broker is needed.
"""

import json
from unittest.mock import MagicMock

import pytest
from conftest import STUB_HOST, _raw_command

import main


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


def _pwr_returns(responses: list[str | None]):
    """Build a BmsConnection.send_command replacement that returns *responses*
    in order for successive bare 'pwr' calls (the last entry repeats once
    exhausted), a ``None`` entry meaning "pass through to the real stub", and
    forwards every other command untouched. Also returns the list of every
    command sent, in order, for assertions."""
    real_send_command = main.BmsConnection.send_command
    state = {"bare_calls": 0}
    sent_commands: list[str] = []

    def _wrapper(self, cmd: str) -> str:
        sent_commands.append(cmd)
        if cmd == "pwr":
            idx = min(state["bare_calls"], len(responses) - 1)
            state["bare_calls"] += 1
            resp = responses[idx]
            if resp is None:
                return real_send_command(self, cmd)
            return resp
        return real_send_command(self, cmd)

    return _wrapper, sent_commands


def _corrupt_pwr_rows(raw: str) -> str:
    """Return a real 'pwr' response with every battery data row's voltage
    column replaced by non-numeric junk: header (and thus "Power Volt")
    survives, but every row raises ValueError in parse_pwr and is dropped,
    yielding a valid-looking response that parses to zero batteries."""
    corrupted_lines = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) > 10 and parts[0].isdigit() and "Absent" not in line:
            parts[1] = "ERR"
            corrupted_lines.append(" ".join(parts))
        else:
            corrupted_lines.append(line)
    return "\r\n".join(corrupted_lines)


def _last_state_payload(fake_mqtt_client) -> dict:
    state_calls = [
        c
        for c in fake_mqtt_client.publish.call_args_list
        if c.args[0] == main.STATE_TOPIC
    ]
    assert state_calls, "no state payload was published"
    return json.loads(state_calls[-1].args[1])


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


class TestMainPollLoopDegradedResponses:
    """The fallback at src/main.py's ``if not system.batteries or not
    pwr_parsed:`` (the correctness path when the aggregate 'pwr' table is
    missing or unparseable) is only reachable through main()'s own poll
    loop, never through _fetch_batteries_indexed's own unit tests (those
    call it directly, bypassing the condition entirely). A mutation
    flipping that `or` to `and` still passed the full suite before these
    tests existed.

    ``coul_status`` is the discriminator used throughout: parse_pwr (the
    aggregate table) never sets it, only parse_pwr_indexed (the per-battery
    fallback) does — so its presence in the published payload proves the
    fallback actually ran, not just that plausible-looking data appeared.
    """

    def test_missing_aggregate_header_falls_back_to_indexed_polling(
        self, monkeypatch, stub_server, fake_mqtt_client
    ) -> None:
        monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
        monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
        monkeypatch.setattr(main, "TCP_HOST", STUB_HOST)
        monkeypatch.setattr(main, "TCP_PORT", stub_server)
        monkeypatch.setattr(main, "ENERGY_STATE_FILE", "")
        monkeypatch.setattr(main, "MONITORING_LEVEL", "low")
        monkeypatch.setattr(main, "AUTO_SYNC_TIME", False)
        monkeypatch.setattr(main.time, "sleep", _stop_after_n_poll_sleeps(1))

        wrapper, sent_commands = _pwr_returns(["no header here\r\npylon>"])
        monkeypatch.setattr(main.BmsConnection, "send_command", wrapper)

        with pytest.raises(SystemExit) as exc_info:
            main.main()
        assert exc_info.value.code == 0

        # "Power Volt" never present -> the one retry built into main() fires,
        # then the loop falls back to probing each battery individually.
        assert sent_commands.count("pwr") == 2
        assert "pwr 1" in sent_commands
        assert "pwr 2" in sent_commands

        payload = _last_state_payload(fake_mqtt_client)
        assert len(payload["batteries"]) == 2
        assert payload["batteries"][0]["coul_status"] is not None

    def test_all_invalid_rows_falls_back_to_indexed_polling(
        self, monkeypatch, stub_server, fake_mqtt_client, stub_conn
    ) -> None:
        corrupted = _corrupt_pwr_rows(_raw_command(stub_conn, "pwr"))
        assert "Power Volt" in corrupted  # sanity: header survived corruption

        monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
        monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
        monkeypatch.setattr(main, "TCP_HOST", STUB_HOST)
        monkeypatch.setattr(main, "TCP_PORT", stub_server)
        monkeypatch.setattr(main, "ENERGY_STATE_FILE", "")
        monkeypatch.setattr(main, "MONITORING_LEVEL", "low")
        monkeypatch.setattr(main, "AUTO_SYNC_TIME", False)
        monkeypatch.setattr(main.time, "sleep", _stop_after_n_poll_sleeps(1))

        wrapper, sent_commands = _pwr_returns([corrupted])
        monkeypatch.setattr(main.BmsConnection, "send_command", wrapper)

        with pytest.raises(SystemExit) as exc_info:
            main.main()
        assert exc_info.value.code == 0

        # Header was present (no retry): exactly one bare 'pwr' call, then
        # every row failed to parse and the loop fell back to per-battery.
        assert sent_commands.count("pwr") == 1
        assert "pwr 1" in sent_commands
        assert "pwr 2" in sent_commands

        payload = _last_state_payload(fake_mqtt_client)
        assert len(payload["batteries"]) == 2
        assert payload["batteries"][0]["coul_status"] is not None

    def test_degraded_second_cycle_does_not_republish_stale_battery_list(
        self, monkeypatch, stub_server, fake_mqtt_client
    ) -> None:
        """Cycle 1 is healthy; cycle 2's aggregate 'pwr' comes back headerless.
        With `if not system.batteries or not pwr_parsed:` the fallback must
        run again in cycle 2 regardless of the (still non-empty, now stale)
        battery list left over from cycle 1. The `and` mutation from the
        review would skip the fallback whenever the stale list happens to be
        non-empty, silently republishing cycle 1's readings as cycle 2's.
        """
        monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
        monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
        monkeypatch.setattr(main, "TCP_HOST", STUB_HOST)
        monkeypatch.setattr(main, "TCP_PORT", stub_server)
        monkeypatch.setattr(main, "ENERGY_STATE_FILE", "")
        # "low" keeps cycle 1 aggregate-only (no _enrich_batteries_indexed),
        # so coul_status starts unset and cleanly marks which cycle's
        # batteries came from the indexed fallback.
        monkeypatch.setattr(main, "MONITORING_LEVEL", "low")
        monkeypatch.setattr(main, "AUTO_SYNC_TIME", False)
        monkeypatch.setattr(main.time, "sleep", _stop_after_n_poll_sleeps(2))

        wrapper, sent_commands = _pwr_returns([None, "no header here\r\npylon>"])
        monkeypatch.setattr(main.BmsConnection, "send_command", wrapper)

        with pytest.raises(SystemExit) as exc_info:
            main.main()
        assert exc_info.value.code == 0

        state_calls = [
            c
            for c in fake_mqtt_client.publish.call_args_list
            if c.args[0] == main.STATE_TOPIC
        ]
        assert len(state_calls) == 2
        cycle1 = json.loads(state_calls[0].args[1])
        cycle2 = json.loads(state_calls[1].args[1])

        assert cycle1["batteries"][0]["coul_status"] is None
        assert cycle2["batteries"][0]["coul_status"] is not None
        assert "pwr 1" in sent_commands
        assert "pwr 2" in sent_commands
