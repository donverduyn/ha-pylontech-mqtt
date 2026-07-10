"""Unit tests for main._build_mqtt_client (src/main.py).

Both MQTT clients in this repo (the sidecar and the HA-side coordinator)
connected over plaintext by default with no way to opt into TLS, so a
broker password was sent in the clear whenever the broker itself wasn't
already on a trusted local network. MQTT_TLS opts the sidecar in.
"""

from typing import cast
from unittest.mock import MagicMock, patch

import main


def test_tls_enabled_calls_tls_set(monkeypatch) -> None:
    monkeypatch.setattr(main, "MQTT_TLS", True)
    monkeypatch.setattr(main, "MQTT_USER", "")

    with patch("main.mqtt.Client") as mock_client_cls:
        client = cast(MagicMock, main._build_mqtt_client())

    assert client is mock_client_cls.return_value
    client.tls_set.assert_called_once()


def test_tls_disabled_does_not_call_tls_set(monkeypatch) -> None:
    monkeypatch.setattr(main, "MQTT_TLS", False)
    monkeypatch.setattr(main, "MQTT_USER", "")

    with patch("main.mqtt.Client"):
        client = cast(MagicMock, main._build_mqtt_client())

    client.tls_set.assert_not_called()


def test_credentials_set_when_user_present(monkeypatch) -> None:
    monkeypatch.setattr(main, "MQTT_TLS", False)
    monkeypatch.setattr(main, "MQTT_USER", "alice")
    monkeypatch.setattr(main, "MQTT_PASS", "secret")

    with patch("main.mqtt.Client"):
        client = cast(MagicMock, main._build_mqtt_client())

    client.username_pw_set.assert_called_once_with("alice", "secret")


def test_last_will_is_always_set(monkeypatch) -> None:
    monkeypatch.setattr(main, "MQTT_TLS", False)
    monkeypatch.setattr(main, "MQTT_USER", "")

    with patch("main.mqtt.Client"):
        client = cast(MagicMock, main._build_mqtt_client())

    client.will_set.assert_called_once_with(main.AVAIL_TOPIC, "offline", retain=True)
