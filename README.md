# Pylontech Integration for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Monitor your Pylontech US2000/US3000/US5000 battery stack in Home Assistant via a lightweight **sidecar container** that reads from the BMS and publishes data over MQTT. The HA integration connects to your MQTT broker directly and subscribes to those messages. Direct access to the BMS hardware (the serial port or TCP bridge) is restricted to the sidecar container and never touches Home Assistant at all — the MQTT broker password, however, is stored in HA, since the integration connects to the broker itself rather than routing through Home Assistant's own MQTT integration.

## Architecture

```
┌─────────────────────────┐        MQTT        ┌──────────────────────────────┐
│  pylon2mqtt (Docker)    │ ─────────────────► │  Home Assistant              │
│                         │                    │                              │
│  • Serial / TCP → BMS   │  pylontech/stack/  │  • pylontech_mqtt custom   │
│  • Parser               │  state  (JSON)     │    component subscribes to   │
│  • Energy tracking      │  availability      │    MQTT topic                │
│  • MQTT publish         │                    │  • Sensors, Energy dashboard │
└─────────────────────────┘                    └──────────────────────────────┘
```

The sidecar and HA are completely decoupled. You can run the sidecar on any machine (a Pi, a NAS, the same host as HA) as long as both can reach the same MQTT broker.

## Features

- **No BMS hardware access in HA**: The serial port device path, TCP bridge host, and any hardware-side passwords live only in the sidecar container's environment variables — Home Assistant never talks to the BMS directly. (The MQTT broker password *is* stored in HA, masked in the UI and redacted from diagnostics — see the Setup steps below.)
- **Serial & TCP support**: Connect via a USB-to-RS232 cable or a serial-over-TCP bridge.
- **Energy Dashboard Ready**: `energy_in` / `energy_out` sensors with `total_increasing` state class, ready for the HA Energy dashboard.
- **Per-Battery Monitoring**: Voltage, Current, SOC, Temperature, Status, min/max cell values for each module.
- **Per-Battery Capacity**: Number entity per module lets you tune the kWh capacity for accurate stored-energy calculation.
- **Stable entity identity**: Entities and devices are keyed off the broker host/port/topic, not an internal config-entry ID — deleting and re-adding the integration with the same broker settings keeps your existing entity IDs, history, and dashboard references instead of creating duplicates.
- **Per-module availability**: A battery module (or cell) that drops out of the stack's reported data shows as unavailable instead of silently freezing on its last known values.
- **Stale-connection watchdog**: If the sidecar's poll loop hangs while its MQTT connection stays open, entities are marked unavailable after 5 minutes of silence instead of showing stale data forever.
- **Optional TLS**: Both the sidecar and the HA integration can connect to the broker over TLS.
- **Diagnostic sensors**: Cycle count, SOH, firmware/board/comm versions, charge/discharge counters, fault event counts.
- **Diagnostics download**: Supports HA's built-in "Download diagnostics" for the config entry when reporting issues (broker password is redacted).
- **Automatic reconnection**: The sidecar reconnects to the BMS on failure; the HA coordinator reconnects to MQTT automatically.
- **Local push**: Data is pushed to HA the moment the sidecar receives it — no polling delay inside HA.

> [!WARNING]
> **Disclaimer:** This integration interacts directly with your hardware. Incorrect configuration or usage could potentially cause damage to your batteries or connected devices. The creators and contributors of this integration take **no responsibility** for any damage, data loss, or other issues that may arise from using this software. Use at your own risk.

> [!IMPORTANT]
> This integration is not affiliated with or endorsed by Pylontech.
> It is a completely independent project backed by the community, based on official documentation, commands, and reverse engineering.

> [!NOTE]
> **Disclaimer:** this project has been generated mainly by AI. Even though it has been reviewed and tested by a professional programmer, I feel like it's important to disclose this fact.

## Setup

### Step 1 — Run the sidecar container

The `docker/` directory contains the sidecar. All configuration is through environment variables — no editing of source files is needed.

No pre-built image is published yet, so `docker-compose.yml`'s `build:` context needs the rest of the repo (`docker/pylontech_parser.py` and `docker/structs.py`) alongside it — copying just the compose file will not build. Clone the whole repository onto your Docker host instead:

```bash
git clone https://github.com/donverduyn/ha-pylontech-mqtt.git
cd ha-pylontech-mqtt/docker
```

Then adjust the variables in `docker/docker-compose.yml`:

```yaml
services:
  pylon2mqtt:
    build:
      context: ..              # repo root — needed to copy parser.py & structs.py
      dockerfile: docker/Dockerfile
    restart: unless-stopped
    environment:
      # --- Connection type: "serial" or "tcp" ---
      CONNECTION_TYPE: serial

      # Serial mode
      SERIAL_PORT: /dev/ttyUSB0
      BAUD_RATE: "115200"

      # TCP mode (serial-over-TCP bridge)
      # If you switch to TCP, also comment out the `devices:` block below —
      # it's unconditional, so on a host without /dev/ttyUSB0 it fails
      # container creation before the app starts.
      # CONNECTION_TYPE: tcp
      # TCP_HOST: 192.168.1.100
      # TCP_PORT: "23"

      # --- MQTT broker ---
      MQTT_BROKER: 192.168.1.10
      MQTT_PORT: "1883"
      # MQTT_USER: your_username
      # MQTT_PASS: your_password
      # MQTT_TLS: "true"        # connect over TLS
      MQTT_TOPIC_PREFIX: pylontech/stack   # must match the HA integration setting

      # --- Polling ---
      POLL_INTERVAL: "15"       # seconds between polls
      AUTO_SYNC_TIME: "false"   # "true" to sync BMS clock on startup

    # Only needed for serial mode — comment out (or delete) when using TCP.
    devices:
      - /dev/ttyUSB0:/dev/ttyUSB0

    # Persists cumulative energy_in/energy_out across container recreation
    volumes:
      - energy_state:/data

volumes:
  energy_state:
```

Build and start (from the `docker/` directory):

```bash
docker compose up -d --build
```

The sidecar publishes:
- `pylontech/stack/state` — JSON snapshot of the entire system on every poll
- `pylontech/stack/availability` — `online` / `offline` (LWT)

#### Environment variable reference

| Variable | Default | Description |
|---|---|---|
| `CONNECTION_TYPE` | `serial` | `serial` or `tcp` |
| `SERIAL_PORT` | `/dev/ttyUSB0` | Serial device path (serial mode) |
| `BAUD_RATE` | `115200` | Baud rate (serial mode) |
| `TCP_HOST` | — | Hostname or IP (tcp mode) |
| `TCP_PORT` | `23` | TCP port (tcp mode) |
| `MQTT_BROKER` | *(required)* | MQTT broker hostname or IP |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | — | MQTT username (optional) |
| `MQTT_PASS` | — | MQTT password (optional) |
| `MQTT_TLS` | `false` | `true` to connect to the broker over TLS |
| `MQTT_TOPIC_PREFIX` | `pylontech/stack` | Base topic; `/state` and `/availability` are appended |
| `POLL_INTERVAL` | `15` | Seconds between BMS polls; max `150` — the HA integration marks the device unavailable after 300s without a message, so anything higher will flap availability |
| `AUTO_SYNC_TIME` | `false` | Sync BMS clock to system time on startup |
| `MONITORING_LEVEL` | `medium` | `low`, `medium`, or `high` — how much detail to walk per battery on top of the aggregate `pwr` table. `low`: aggregate `pwr` only. `medium`: adds one `pwr N` round trip per battery for event/fault status the aggregate table doesn't expose. `high`: adds per-battery `bat N` cell polling on top of `medium`, which creates a further 9 HA entities *per cell* — opt into `high` deliberately, since on a large stack (e.g. 16 modules × 15 cells) that's 2,000+ additional entities. Lower levels also mean fewer round trips per poll — useful on larger stacks. |
| `MAX_BATTERIES` | `16` | Upper bound on `pwr N` probes when the aggregate `pwr` response doesn't look valid (some firmware only exposes per-battery data this way) |
| `ENERGY_STATE_FILE` | `/data/energy_state.json` | Where cumulative energy_in/energy_out are persisted; set to `""` to disable. Requires the `/data` volume mount shown above to survive container recreation. |

### Step 2 — Install the HA integration

> [!NOTE]
> Requires **Home Assistant 2025.4 or newer** — HA's own built-in `mqtt` component pins `paho-mqtt==1.6.1` on every release before 2025.3.0, which conflicts with this integration's `paho-mqtt>=2.0.0` requirement (needed for the `paho.mqtt.enums` module). The entire 2025.3.x branch is itself uninstallable from scratch (it pins an `aiohttp` release since yanked from PyPI), so 2025.4.0 is the first version that actually works — see `hacs.json`. It also needs the config entry reconfigure flow used by Step 3's Reconfigure option, available since 2024.11.

#### Via HACS (Recommended)

1. Ensure you have [HACS](https://hacs.xyz/) installed.
2. Go to **HACS > Integrations**.
3. Click the **3 dots** (top right) > **Custom repositories**.
4. Add the URL of this repository, category **Integration**.
5. Find and install **Pylontech**.
6. Restart Home Assistant.

#### Manual installation

1. Copy the `custom_components/pylontech_mqtt` folder into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

### Step 3 — Add the integration in HA

1. Go to **Settings** > **Devices & Services** > **+ Add Integration**.
2. Search for **Pylontech**.
3. Enter your MQTT broker details:
   - **Broker Host / IP**
   - **Broker Port** (default `1883`)
   - **Username** and **Password** (leave blank if not required)
   - **Topic Prefix** — must match `MQTT_TOPIC_PREFIX` from the sidecar (default `pylontech/stack`)
   - **Use TLS** — enable if the broker requires TLS (matches sidecar's `MQTT_TLS`)
4. Click **Submit**. HA will verify the broker is reachable before saving.

> [!NOTE]
> Entities will show as **unavailable** until the sidecar publishes its first MQTT message. This is expected — start or check the sidecar container if they stay unavailable.

To change the broker host, port, credentials, or topic prefix later, use **Reconfigure** from the integration entry's **⋮** menu in **Settings** > **Devices & Services** — this replaces the stored connection details in place rather than layering a separate copy on top.

## Battery Capacity Configuration

Each detected battery module gets its own **Number entity** (under the module's device page) to configure its capacity in kWh. This is used to calculate the stored energy per module and the system total.

Default values by model:

| Model | Capacity |
|---|---|
| US2000 | 2.4 kWh |
| US3000 | 3.5 kWh |
| US5000 | 4.8 kWh |

## Energy Dashboard Setup

1. Go to **Settings** > **Dashboards** > **Energy**.
2. Find **Battery Systems** and click **Add Battery System**.
3. For **Energy going in to the battery**, select `sensor.pylontech_stack_system_energy_charged`.
4. For **Energy coming out of the battery**, select `sensor.pylontech_stack_system_energy_discharged`.
5. Click **Save**.

> [!NOTE]
> Energy values (`energy_in` / `energy_out`) are tracked inside the sidecar container and persisted to `ENERGY_STATE_FILE` (default `/data/energy_state.json`) by default, so they survive both plain restarts and container recreation as long as the `/data` volume shown in Step 1 exists. They only reset to zero if persistence is disabled (`ENERGY_STATE_FILE=""`) or that volume is removed. Either way, the HA Energy dashboard handles cumulative tracking via its own long-term statistics — a counter reset is treated as the start of a new cycle, not corruption of your historical totals.

## Hardware Notes

Connect to the **Console** port of the *master* battery module using a USB-to-RS232 (or RS485, depending on model) cable.

Ensure the DIP switches are configured for the correct baud rate. For US2000/US3000/US5000, **all DIP switches OFF** selects the default baud rate of **115200**.

![DIP Switches](assets/dip-switches.jpg)
*Pylontech US2000 with all DIP switches OFF*

## Upgrading from v1.x (direct serial/TCP)

Version 2.0 replaced the original direct serial/TCP connection from HA with the MQTT sidecar model described above. The sidecar and the HA integration are versioned and released together — install matching versions of both (see the sidecar's `sidecar_version` and the integration's `schema_version` compatibility check in Troubleshooting below).

**Existing config entries cannot be migrated automatically.** After upgrading:

1. Delete the old integration entry in **Settings** > **Devices & Services**.
2. Deploy the sidecar container (Step 1 above).
3. Re-add the integration pointing to your MQTT broker (Steps 2–3 above).

## Troubleshooting

- **Entities stay unavailable**: Check that the sidecar container is running (`docker compose logs -f pylon2mqtt`) and that `MQTT_TOPIC_PREFIX` matches in both the container and the HA integration.
- **HA log shows "Rejecting malformed MQTT state payload — schema_version ... is not supported"**: The sidecar container and the HA integration were upgraded independently and are now on incompatible releases (the sidecar is built from source and the integration installs via HACS, so nothing keeps their versions in lockstep automatically). Rebuild the sidecar (`docker compose up -d --build`) from the same commit/tag as the installed integration version.
- **Sidecar can't connect to BMS**: Verify the serial port path or TCP host/port. For serial mode, confirm the `devices:` mapping in the compose file includes the correct device node.
- **Serial permissions (HA Core/Docker)**: Ensure the USB device is passed through to the sidecar container via the `devices:` key in `docker-compose.yml`. The container runs as a non-root user in the `dialout` group, which matches the default ownership/permissions Linux assigns to USB-serial adapters; if your host uses a different group for the device, add `group_add: ["<host-gid>"]` to `docker-compose.yml` (find the GID with `getent group dialout` on the host).
- **`energy_state.json` stops persisting after upgrading**: Versions built from a Dockerfile predating the non-root sidecar user wrote `/data` as root. After upgrading, the new non-root user can't write to that existing volume (writes fail silently — a warning is logged, and energy counters just won't survive a restart). Fix it once with `docker compose exec -u root pylon2mqtt chown -R pylon2mqtt:pylon2mqtt /data` (or delete and let the sidecar recreate the volume, losing accumulated `energy_in`/`energy_out` — safe if you rely on the HA Energy dashboard's own long-term statistics, per the note above).
- **Wrong data**: Check that you are connected to the **Console** port of the master battery, not the CAN or RS485 port.

