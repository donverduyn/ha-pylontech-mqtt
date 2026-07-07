"""End-to-end tests for the *built* sidecar image (docker/Dockerfile).

Every other test in this suite either drives BmsConnection/PylontechParser
directly or unit-tests coordinator logic with hand-built payloads. These
tests bring up docker/docker-compose.test.yml — the sidecar image, a
containerized BMS stub, and a real Mosquitto broker on one Docker network —
and verify what actually crosses the wire.

Running the image rather than `python main.py` on a dev interpreter is the
entire point: it is the only place the shipped artifact is executed, so it
is what catches packaging bugs a subprocess run structurally cannot — a
module missing from the Dockerfile's COPY list, dependency versions
hardcoded in the image drifting from the tested lockfile, the pinned
runtime drifting from the tested interpreter (this image once sat on
Python 3.11 while every check ran 3.13), /data write failures for the
non-root user, and PID-1 signal handling.

Requires a Docker daemon with the compose plugin; skipped automatically if
`docker` isn't on PATH. Works both on a plain host (CI) and from inside a
devcontainer using docker-outside-of-docker — see _broker_host() for the
one place that difference leaks in.

Marked "e2e" and excluded from the default `pytest` run (see addopts in
pyproject.toml). CI runs this after building the image, in tests.yaml's
docker job; run locally with `pytest -m e2e` (first run builds the images).
"""

import json
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
import pytest_socket
from paho.mqtt.enums import CallbackAPIVersion

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        shutil.which("docker") is None,
        reason="requires the 'docker' CLI (with the compose plugin) on PATH",
    ),
]


@pytest.fixture(autouse=True)
def _allow_compose_stack_hosts() -> None:
    """Widen pytest-socket's connect allowlist for these tests.

    pytest-homeassistant-custom-component's pytest_runtest_setup hook
    unconditionally resets the allowlist to ["127.0.0.1"] before every test
    (any --allow-hosts in addopts is overridden), which blocks reaching the
    compose stack's published broker port at host.docker.internal under
    docker-outside-of-docker. Fixtures run after that hook, so re-widening
    here wins; unresolvable names (host.docker.internal on CI/plain hosts)
    are silently dropped by pytest-socket, and its own per-test teardown
    removes the widened restriction again.
    """
    pytest_socket.socket_allow_hosts(
        ["127.0.0.1", "localhost", "host.docker.internal"],
        allow_unix_socket=True,
    )


_COMPOSE_FILE = Path(__file__).parent.parent / "docker" / "docker-compose.test.yml"
_TOPIC_PREFIX = "pylontech/e2e"  # fixed in docker-compose.test.yml


def _compose(
    project: str, *args: str, check: bool = True
) -> "subprocess.CompletedProcess[str]":
    """Run `docker compose` against the test stack under *project*'s namespace."""
    return subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "-p", project, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _broker_host(port: int) -> str:
    """Return the address where mosquitto's published port is reachable.

    On a plain host (CI) a published port binds on localhost. From inside a
    devcontainer using docker-outside-of-docker, the port is published on
    the *host* VM instead — reachable as host.docker.internal — because the
    compose stack runs as sibling containers on the host daemon, not
    children of the devcontainer. Probing beats configuration: the same
    test file works in both places with no environment flag to forget.
    """
    for host in ("127.0.0.1", "host.docker.internal"):
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return host
        except OSError:
            continue
    raise RuntimeError(
        f"mosquitto's published port {port} is not reachable via localhost "
        "or host.docker.internal"
    )


@pytest.fixture
def stack() -> Generator[tuple[str, str, int]]:
    """Bring up the compose stack under a unique project; yield (project, host, port).

    A fresh uuid-suffixed project name per test gives each one its own
    network, broker, and containers — no retained-message bleed between
    tests and no port/name collisions with a concurrently running session
    (the same isolation policy as the unit suite's OS-assigned stub ports).
    """
    project = f"pylon-e2e-{uuid.uuid4().hex[:8]}"
    try:
        _compose(project, "up", "--build", "--detach")
        port_line = _compose(project, "port", "mosquitto", "1883").stdout.strip()
        port = int(port_line.rsplit(":", 1)[1])
        yield project, _broker_host(port), port
    finally:
        # Captured by pytest; shown only when the test fails, where the
        # sidecar/stub/broker logs are usually the whole diagnosis.
        logs = _compose(project, "logs", "--no-color", check=False)
        print(logs.stdout)
        _compose(project, "down", "--volumes", "--remove-orphans", "-t", "5")


class _Subscriber:
    """Collects the latest retained payload per topic over a real MQTT connection."""

    def __init__(self, host: str, port: int, topics: list[str]) -> None:
        self.received: dict[str, str] = {}
        self._client = mqtt.Client(CallbackAPIVersion.VERSION2)
        self._client.on_message = self._on_message
        self._client.connect(host, port, 60)
        for topic in topics:
            self._client.subscribe(topic)
        self._client.loop_start()

    def _on_message(self, client, userdata, msg) -> None:
        self.received[msg.topic] = msg.payload.decode()

    def wait_for(self, topic: str, value: str | None, timeout: float = 60.0) -> None:
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


def test_image_publishes_valid_state_and_shuts_down_cleanly(
    stack: tuple[str, str, int],
) -> None:
    """The built image connects, publishes a schema-valid state payload and
    comes online; `docker stop` (SIGTERM to PID 1) drives the
    _clean_shutdown path, publishes 'offline', and exits 0."""
    project, host, port = stack
    sub = _Subscriber(
        host, port, [f"{_TOPIC_PREFIX}/state", f"{_TOPIC_PREFIX}/availability"]
    )
    try:
        sub.wait_for(f"{_TOPIC_PREFIX}/availability", "online")
        sub.wait_for(f"{_TOPIC_PREFIX}/state", None)

        state = json.loads(sub.received[f"{_TOPIC_PREFIX}/state"])
        assert state["schema_version"] == 1
        assert state["sidecar_version"]
        assert len(state["batteries"]) == 2
        assert state["voltage"] > 0
        # energy_in/out present means the non-root user's /data write path
        # (EnergyTracker persistence) didn't blow up inside the container.
        assert state["energy_in"] >= 0
        assert state["energy_out"] >= 0

        # Resolve the container id before stopping — `compose ps -q` only
        # lists running containers.
        sidecar_id = _compose(project, "ps", "-q", "sidecar").stdout.strip()
        assert sidecar_id, "sidecar container not found"

        sub.received.pop(f"{_TOPIC_PREFIX}/availability", None)
        # `docker stop` sends SIGTERM to PID 1 — the exact signal `docker
        # stop`/compose shutdown delivers in production — then SIGKILL after
        # the timeout. Exit code 0 therefore proves the SIGTERM handler ran
        # a clean shutdown as PID 1; 137 would mean it hung and was killed.
        _compose(project, "stop", "-t", "15", "sidecar")
        exit_code = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.ExitCode}}", sidecar_id],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert exit_code == "0"

        sub.wait_for(f"{_TOPIC_PREFIX}/availability", "offline")
    finally:
        sub.close()


def test_bms_disconnect_marks_offline_then_recovers_on_reconnect(
    stack: tuple[str, str, int],
) -> None:
    """Stopping the BMS stub container mid-poll must drive the
    `except (serial.SerialException, OSError)` reconnect path: the sidecar
    publishes 'offline', keeps retrying, and resumes publishing 'online' +
    valid state once the BMS is reachable again."""
    project, host, port = stack
    sub = _Subscriber(
        host, port, [f"{_TOPIC_PREFIX}/state", f"{_TOPIC_PREFIX}/availability"]
    )
    try:
        sub.wait_for(f"{_TOPIC_PREFIX}/availability", "online")

        # Simulate the BMS disappearing (cable unplugged / device power loss).
        _compose(project, "stop", "-t", "5", "stub")
        sub.received.pop(f"{_TOPIC_PREFIX}/availability", None)
        sub.wait_for(f"{_TOPIC_PREFIX}/availability", "offline")

        # Bring the same container back: it keeps its network alias and
        # port, so the sidecar's own retry loop (no restart needed) picks it
        # back up — the container equivalent of plugging the cable back in.
        _compose(project, "start", "stub")
        sub.received.pop(f"{_TOPIC_PREFIX}/availability", None)
        sub.received.pop(f"{_TOPIC_PREFIX}/state", None)
        sub.wait_for(f"{_TOPIC_PREFIX}/availability", "online")
        sub.wait_for(f"{_TOPIC_PREFIX}/state", None)
    finally:
        sub.close()
