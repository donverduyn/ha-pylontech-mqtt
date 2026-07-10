"""Unit tests for main()'s startup config validation (src/main.py).

Every check here runs and exits(1) before any socket/MQTT connection is
attempted, so these are safe to exercise directly without mocking I/O.
"""

import pytest

import main


def test_int_env_falls_back_to_default_on_non_numeric_value(
    monkeypatch, caplog
) -> None:
    """A non-numeric env var (e.g. BAUD_RATE=fast) must log and fall back to
    the default rather than crashing the whole process on int()."""
    import logging

    monkeypatch.setenv("BAUD_RATE", "fast")

    with caplog.at_level(logging.ERROR, logger="pylon2mqtt"):
        result = main._int_env("BAUD_RATE", 115200)

    assert result == 115200
    assert "Invalid value for BAUD_RATE='fast'" in caplog.text


def test_missing_mqtt_broker_exits_before_connecting(monkeypatch, caplog) -> None:
    """MQTT_BROKER is the one setting with no default — an empty value must
    fail fast rather than reaching client.connect() with an empty host."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "")

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "MQTT_BROKER environment variable is required" in caplog.text


def test_empty_serial_port_exits_before_connecting(monkeypatch, caplog) -> None:
    """CONNECTION_TYPE=serial (the default) with an explicitly blanked-out
    SERIAL_PORT must fail fast rather than reaching serial.Serial("") with
    an opaque error."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "CONNECTION_TYPE", "serial")
    monkeypatch.setattr(main, "SERIAL_PORT", "")

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "SERIAL_PORT must not be empty" in caplog.text


@pytest.mark.parametrize("bad_baud", [0, -115200])
def test_non_positive_baud_rate_exits_before_connecting(
    monkeypatch, caplog, bad_baud
) -> None:
    """A non-positive BAUD_RATE (e.g. an env var typo) must fail fast rather
    than reaching pyserial's own, less legible validation error."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "CONNECTION_TYPE", "serial")
    monkeypatch.setattr(main, "BAUD_RATE", bad_baud)

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "BAUD_RATE must be a positive integer" in caplog.text


@pytest.mark.parametrize("bad_port", [0, -1, 65536])
def test_mqtt_port_out_of_range_exits_before_connecting(
    monkeypatch, caplog, bad_port
) -> None:
    """An out-of-range MQTT_PORT must fail fast rather than reaching
    client.connect() and failing with an opaque error."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "MQTT_PORT_ENV", bad_port)

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "MQTT_PORT must be between 1 and 65535" in caplog.text


def test_invalid_connection_type_exits_before_connecting(monkeypatch, caplog) -> None:
    """A typo like CONNECTION_TYPE=tpc must fail validation up front rather
    than silently falling through to the serial branch (every check in the
    codebase is `if CONNECTION_TYPE == "tcp": ... else: # serial`), which
    would produce misleading serial-port errors for what was meant to be a
    TCP connection."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "CONNECTION_TYPE", "tpc")

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "CONNECTION_TYPE must be" in caplog.text


def test_valid_connection_types_pass_this_check(monkeypatch, caplog) -> None:
    """ "tcp" must not be rejected by the new guard itself — the SystemExit(1)
    it still hits next (TCP_HOST is left empty) must come from the
    *existing* TCP_HOST check, not a false-positive CONNECTION_TYPE
    rejection."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
    monkeypatch.setattr(main, "TCP_HOST", "")

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "TCP_HOST is required" in caplog.text
    assert "CONNECTION_TYPE must be" not in caplog.text


def test_invalid_monitoring_level_exits_before_connecting(monkeypatch, caplog) -> None:
    """A typo like MONITORING_LEVEL=med must fail validation up front rather
    than silently falling through to some other detail level."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "MONITORING_LEVEL", "med")

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "MONITORING_LEVEL must be" in caplog.text


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_valid_monitoring_levels_pass_this_check(monkeypatch, caplog, level) -> None:
    """Each of the three valid levels must not be rejected by the new guard
    itself — the SystemExit(1) it still hits next (invalid POLL_INTERVAL)
    must come from the *existing* POLL_INTERVAL check, not a false-positive
    MONITORING_LEVEL rejection."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "MONITORING_LEVEL", level)
    monkeypatch.setattr(main, "POLL_INTERVAL", 0)

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "POLL_INTERVAL must be" in caplog.text
    assert "MONITORING_LEVEL must be" not in caplog.text


def test_poll_interval_above_max_exits_before_connecting(monkeypatch, caplog) -> None:
    """A POLL_INTERVAL above _MAX_POLL_INTERVAL must be rejected up front —
    otherwise it silently produces a setup that flaps availability against
    the HA integration's fixed 300s staleness watchdog."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "POLL_INTERVAL", main._MAX_POLL_INTERVAL + 1)

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "POLL_INTERVAL must be at most" in caplog.text


@pytest.mark.parametrize("bad_port", [0, -1, 65536, 100000])
def test_tcp_port_out_of_range_exits_before_connecting(
    monkeypatch, caplog, bad_port
) -> None:
    """An out-of-range TCP_PORT (e.g. copy-pasted with a stray digit) must
    fail fast rather than reaching socket.connect() and failing with an
    opaque OSError."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "CONNECTION_TYPE", "tcp")
    monkeypatch.setattr(main, "TCP_HOST", "192.168.1.100")
    monkeypatch.setattr(main, "TCP_PORT", bad_port)

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "TCP_PORT must be between 1 and 65535" in caplog.text


def test_tcp_port_out_of_range_ignored_in_serial_mode(monkeypatch, caplog) -> None:
    """TCP_PORT is only meaningful in tcp mode — an out-of-range value must
    not block a serial-mode setup that never uses it."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "CONNECTION_TYPE", "serial")
    monkeypatch.setattr(main, "TCP_PORT", 0)
    monkeypatch.setattr(main, "POLL_INTERVAL", 0)

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "POLL_INTERVAL must be" in caplog.text
    assert "TCP_PORT must be" not in caplog.text


@pytest.mark.parametrize(
    "bad_prefix",
    [
        "",
        " pylontech/stack",
        "pylontech/stack ",
        "/pylontech/stack",
        "pylontech/stack/",
        "pylontech/#",
        "pylontech/+/stack",
    ],
)
def test_invalid_topic_prefix_exits_before_connecting(
    monkeypatch, caplog, bad_prefix
) -> None:
    """A MQTT_TOPIC_PREFIX set directly on the sidecar (bypassing the HA
    config flow's own _invalid_topic_prefix check) must be rejected up front
    rather than raising deep inside paho-mqtt at publish time."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "MQTT_TOPIC_PREFIX", bad_prefix)

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "MQTT_TOPIC_PREFIX" in caplog.text
    assert "is invalid" in caplog.text


def test_valid_topic_prefix_passes_this_check(monkeypatch, caplog) -> None:
    """A well-formed prefix must not be rejected by the new guard itself."""
    import logging

    monkeypatch.setattr(main, "MQTT_BROKER", "localhost")
    monkeypatch.setattr(main, "MQTT_TOPIC_PREFIX", "pylontech/stack")
    monkeypatch.setattr(main, "POLL_INTERVAL", 0)

    with (
        caplog.at_level(logging.ERROR, logger="pylon2mqtt"),
        pytest.raises(SystemExit) as exc_info,
    ):
        main.main()

    assert exc_info.value.code == 1
    assert "POLL_INTERVAL must be" in caplog.text
    assert "MQTT_TOPIC_PREFIX" not in caplog.text
