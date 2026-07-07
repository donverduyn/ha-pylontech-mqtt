"""End-to-end tests for the sidecar's real poll loop (docker/main.py).

Every other test in this suite either drives BmsConnection/PylontechParser
directly or unit-tests coordinator logic with hand-built payloads — main()'s
poll loop itself (MQTT connect/publish, the BMS reconnect path, clean
shutdown) is never actually executed. These tests run the real
``docker/main.py`` entry point as a subprocess against a real (local)
Mosquitto broker and the existing ``scripts/pylon_stub.py`` BMS emulator,
and verify what actually crosses the wire.

Requires a local ``mosquitto`` binary (``apt install mosquitto`` /
``brew install mosquitto``); skipped automatically if it isn't on PATH.

Marked "e2e" and excluded from the default `pytest` run (see addopts in
pyproject.toml) since it spawns real subprocesses and takes several seconds,
unlike the rest of the suite. Run explicitly with `pytest -m e2e`.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
from conftest import StubProcess, start_stub
from paho.mqtt.enums import CallbackAPIVersion

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        shutil.which("mosquitto") is None,
        reason="requires the 'mosquitto' broker binary on PATH",
    ),
]

_ROOT = Path(__file__).parent.parent
_HOST = "127.0.0.1"


def _free_port() -> int:
    """Reserve an OS-assigned free port and release it for immediate reuse.

    For subprocesses that can't report a self-chosen port the way
    pylon_stub.py does (mosquitto needs its listener port written into its
    config file up front). Racy in principle between close and reuse, but
    an OS-assigned ephemeral port is not handed out again until the pool
    wraps around — unlike a hardcoded port, which collides with a parallel
    test session picking the same constant every time.
    """
    with socket.socket() as s:
        s.bind((_HOST, 0))
        return s.getsockname()[1]


def _wait_for_port(
    host: str, port: int, proc: subprocess.Popen, timeout: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else b""
            raise RuntimeError(
                f"process exited with code {proc.returncode} before opening "
                f"{host}:{port}. Output:\n{out.decode(errors='replace')}"
            )
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"{host}:{port} did not open within {timeout}s")


@pytest.fixture
def mosquitto_broker(tmp_path: Path) -> Generator[int]:
    """A real, unauthenticated local Mosquitto broker for the sidecar to publish to."""
    broker_port = _free_port()
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(f"listener {broker_port} {_HOST}\nallow_anonymous true\n")
    proc = subprocess.Popen(
        ["mosquitto", "-c", str(conf)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_port(_HOST, broker_port, proc)
        yield broker_port
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _spawn_stub(port: int = 0) -> StubProcess:
    """Start the BMS stub; port 0 (default) lets the OS assign a free one."""
    return start_stub("--batteries", "2", "--model", "US5000", port=port)


def _spawn_sidecar(
    *, broker_port: int, stub_port: int, topic_prefix: str, energy_state_file: str
) -> subprocess.Popen:
    """Launch the real docker/main.py entry point as a subprocess.

    Run directly (not imported) so its module-level env-var reads pick up
    this test's configuration fresh, and so it exercises the exact code path
    that runs in production, including __main__ dispatch.
    """
    env = {
        **os.environ,
        "CONNECTION_TYPE": "tcp",
        "TCP_HOST": _HOST,
        "TCP_PORT": str(stub_port),
        "MQTT_BROKER": _HOST,
        "MQTT_PORT": str(broker_port),
        "MQTT_TOPIC_PREFIX": topic_prefix,
        "POLL_INTERVAL": "1",
        "MONITORING_LEVEL": "low",
        "ENERGY_STATE_FILE": energy_state_file,
    }
    return subprocess.Popen(
        [sys.executable, str(_ROOT / "docker" / "main.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


class _Subscriber:
    """Collects the latest retained payload per topic over a real MQTT connection."""

    def __init__(self, broker_port: int, topics: list[str]) -> None:
        self.received: dict[str, str] = {}
        self._client = mqtt.Client(CallbackAPIVersion.VERSION2)
        self._client.on_message = self._on_message
        self._client.connect(_HOST, broker_port, 60)
        for topic in topics:
            self._client.subscribe(topic)
        self._client.loop_start()

    def _on_message(self, client, userdata, msg) -> None:
        self.received[msg.topic] = msg.payload.decode()

    def wait_for(self, topic: str, value: str | None, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if value is None:
                if topic in self.received:
                    return
            elif self.received.get(topic) == value:
                return
            time.sleep(0.2)
        raise AssertionError(
            f"Timed out waiting for {topic}={value!r}; last seen: {self.received!r}"
        )

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


def test_happy_path_publishes_valid_state_and_shuts_down_cleanly(
    mosquitto_broker: int, tmp_path: Path
) -> None:
    """Sidecar connects, publishes a schema-valid state payload and comes
    online, then a SIGTERM (as sent by `docker stop`) drives the
    _clean_shutdown path and publishes 'offline' before exiting 0."""
    topic_prefix = "pylontech/e2e-happy"
    stub = _spawn_stub()
    assert stub.port is not None
    sidecar = None
    sub = _Subscriber(
        mosquitto_broker, [f"{topic_prefix}/state", f"{topic_prefix}/availability"]
    )
    try:
        sidecar = _spawn_sidecar(
            broker_port=mosquitto_broker,
            stub_port=stub.port,
            topic_prefix=topic_prefix,
            energy_state_file=str(tmp_path / "energy.json"),
        )

        sub.wait_for(f"{topic_prefix}/availability", "online")
        sub.wait_for(f"{topic_prefix}/state", None)

        state = json.loads(sub.received[f"{topic_prefix}/state"])
        assert state["schema_version"] == 1
        assert state["sidecar_version"]
        assert len(state["batteries"]) == 2
        assert state["voltage"] > 0

        sub.received.pop(f"{topic_prefix}/availability", None)
        sidecar.send_signal(signal.SIGTERM)
        exit_code = sidecar.wait(timeout=15)
        assert exit_code == 0

        sub.wait_for(f"{topic_prefix}/availability", "offline")
    finally:
        sub.close()
        if sidecar is not None and sidecar.poll() is None:
            sidecar.terminate()
            sidecar.wait(timeout=5)
        stub.stop()


def test_bms_disconnect_marks_offline_then_recovers_on_reconnect(
    mosquitto_broker: int, tmp_path: Path
) -> None:
    """Killing the BMS stub mid-poll must drive the
    `except (serial.SerialException, OSError, IOError)` reconnect path: the
    sidecar publishes 'offline', keeps retrying, and resumes publishing
    'online' + valid state once the BMS is reachable again."""
    topic_prefix = "pylontech/e2e-reconnect"
    stub = _spawn_stub()
    # Remember the OS-assigned port: the revived stub below must come back on
    # this exact port, since it's baked into the running sidecar's TCP_PORT.
    stub_port = stub.port
    assert stub_port is not None
    sidecar = None
    sub = _Subscriber(
        mosquitto_broker, [f"{topic_prefix}/state", f"{topic_prefix}/availability"]
    )
    try:
        sidecar = _spawn_sidecar(
            broker_port=mosquitto_broker,
            stub_port=stub_port,
            topic_prefix=topic_prefix,
            energy_state_file=str(tmp_path / "energy.json"),
        )
        sub.wait_for(f"{topic_prefix}/availability", "online")

        # Simulate the BMS disappearing (cable unplugged / device power loss).
        stub.stop()
        sub.received.pop(f"{topic_prefix}/availability", None)
        sub.wait_for(f"{topic_prefix}/availability", "offline", timeout=20)

        # Bring it back on the same port; the sidecar's own retry loop
        # (no restart needed) should pick it back up.
        stub = _spawn_stub(stub_port)
        sub.received.pop(f"{topic_prefix}/availability", None)
        sub.received.pop(f"{topic_prefix}/state", None)
        sub.wait_for(f"{topic_prefix}/availability", "online", timeout=20)
        sub.wait_for(f"{topic_prefix}/state", None)
    finally:
        sub.close()
        if sidecar is not None and sidecar.poll() is None:
            sidecar.terminate()
            sidecar.wait(timeout=5)
        stub.stop()
