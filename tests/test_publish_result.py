"""Unit tests for main._publish_succeeded (src/main.py).

client.publish() returns MQTT_ERR_NO_CONN rather than raising when the
client isn't currently connected, so the poll loop must check the return
value before logging a reading as "Published" — otherwise a disconnected
sidecar silently claims delivery for data that never left the process.
"""

from dataclasses import dataclass

from paho.mqtt.enums import MQTTErrorCode

from main import _publish_succeeded


@dataclass
class _Info:
    rc: MQTTErrorCode


def _info(rc: MQTTErrorCode) -> _Info:
    return _Info(rc=rc)


def test_all_successful_returns_true() -> None:
    assert _publish_succeeded(
        _info(MQTTErrorCode.MQTT_ERR_SUCCESS), _info(MQTTErrorCode.MQTT_ERR_SUCCESS)
    )


def test_any_failure_returns_false() -> None:
    assert not _publish_succeeded(
        _info(MQTTErrorCode.MQTT_ERR_SUCCESS), _info(MQTTErrorCode.MQTT_ERR_NO_CONN)
    )


def test_no_connection_returns_false() -> None:
    assert not _publish_succeeded(_info(MQTTErrorCode.MQTT_ERR_NO_CONN))


def test_single_success_returns_true() -> None:
    assert _publish_succeeded(_info(MQTTErrorCode.MQTT_ERR_SUCCESS))
