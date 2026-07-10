"""Unit tests for main._clean_shutdown (src/main.py).

_clean_shutdown is the single graceful-shutdown path shared by every point
in the poll loop that can observe a KeyboardInterrupt (raised by the SIGTERM
handler) — including the POLL_INTERVAL sleep between polls, which used to
sit outside any try/except and would let a SIGTERM received during that
sleep bypass "offline" publication, MQTT shutdown, and BMS cleanup entirely.
"""

from unittest.mock import MagicMock

import pytest

from main import _clean_shutdown


def test_clean_shutdown_exits_with_status_zero() -> None:
    client = MagicMock()
    bms = MagicMock()
    energy = MagicMock()

    with pytest.raises(SystemExit) as exc_info:
        _clean_shutdown(client, bms, energy)

    assert exc_info.value.code == 0


def test_clean_shutdown_calls_expected_sequence() -> None:
    client = MagicMock()
    bms = MagicMock()
    energy = MagicMock()

    with pytest.raises(SystemExit):
        _clean_shutdown(client, bms, energy)

    assert client.publish.called
    publish_args, publish_kwargs = client.publish.call_args
    assert publish_args[1] == "offline"
    assert publish_kwargs.get("retain") is True

    client.publish.return_value.wait_for_publish.assert_called_once()
    client.loop_stop.assert_called_once()
    client.disconnect.assert_called_once()
    bms.close.assert_called_once()
    energy.flush.assert_called_once()


def test_clean_shutdown_still_cleans_up_when_publish_wait_raises() -> None:
    """A broker outage makes wait_for_publish() raise (e.g. MQTT_ERR_NO_CONN);
    loop_stop/disconnect/bms.close/energy.flush must still run and the
    process must still exit 0 instead of crashing out mid-shutdown."""
    client = MagicMock()
    client.publish.return_value.wait_for_publish.side_effect = RuntimeError(
        "Message publish failed: No connection"
    )
    bms = MagicMock()
    energy = MagicMock()

    with pytest.raises(SystemExit) as exc_info:
        _clean_shutdown(client, bms, energy)

    assert exc_info.value.code == 0
    client.loop_stop.assert_called_once()
    client.disconnect.assert_called_once()
    bms.close.assert_called_once()
    energy.flush.assert_called_once()
