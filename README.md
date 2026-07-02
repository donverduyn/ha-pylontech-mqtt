# Pylontech Integration for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Monitor your Pylontech US2000/US3000/US5000 battery stack in Home Assistant via a lightweight **sidecar container** that reads from the BMS and publishes data over MQTT. The HA integration subscribes to those MQTT messages — no serial port or TCP password ever stored inside Home Assistant.

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

- **No credentials in HA**: Serial port, TCP host, and any passwords live only in the sidecar container's environment variables.
- **Serial & TCP support**: Connect via a USB-to-RS232 cable or a serial-over-TCP bridge.
- **Energy Dashboard Ready**: `energy_in` / `energy_out` sensors with `total_increasing` state class, ready for the HA Energy dashboard.
- **Per-Battery Monitoring**: Voltage, Current, SOC, Temperature, Status, min/max cell values for each module.
- **Per-Battery Capacity**: Number entity per module lets you tune the kWh capacity for accurate stored-energy calculation.
- **Diagnostics**: Cycle count, SOH, firmware/board/comm versions, charge/discharge counters, fault event counts.
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

Copy `docker/docker-compose.yml` to your Docker host and adjust the variables:

```yaml
services:
  pylon2mqtt:
    build: .          # or use an image if you publish one
    restart: unless-stopped
    environment:
      # --- Connection type: "serial" or "tcp" ---
      CONNECTION_TYPE: serial

      # Serial mode
      SERIAL_PORT: /dev/ttyUSB0
      BAUD_RATE: "115200"

      # TCP mode (serial-over-TCP bridge)
      # CONNECTION_TYPE: tcp
      # TCP_HOST: 192.168.1.100
      # TCP_PORT: "23"

      # --- MQTT broker ---
      MQTT_BROKER: 192.168.1.10
      MQTT_PORT: "1883"
      # MQTT_USER: your_username
      # MQTT_PASS: your_password
      MQTT_TOPIC_PREFIX: pylontech/stack   # must match the HA integration setting

      # --- Polling ---
      POLL_INTERVAL: "15"       # seconds between polls
      AUTO_SYNC_TIME: "false"   # "true" to sync BMS clock on startup

    devices:
      - /dev/ttyUSB0:/dev/ttyUSB0   # serial mode only
```

Build and start:

```bash
cd docker
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
| `MQTT_TOPIC_PREFIX` | `pylontech/stack` | Base topic; `/state` and `/availability` are appended |
| `POLL_INTERVAL` | `15` | Seconds between BMS polls |
| `AUTO_SYNC_TIME` | `false` | Sync BMS clock to system time on startup |

### Step 2 — Install the HA integration

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
4. Click **Submit**. HA will verify the broker is reachable before saving.

> [!NOTE]
> Entities will show as **unavailable** until the sidecar publishes its first MQTT message. This is expected — start or check the sidecar container if they stay unavailable.

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
> Energy values (`energy_in` / `energy_out`) are tracked inside the sidecar container and reset when the container restarts. The HA Energy dashboard handles cumulative tracking via its own long-term statistics, so container restarts will not corrupt your historical totals.

## Hardware Notes

Connect to the **Console** port of the *master* battery module using a USB-to-RS232 (or RS485, depending on model) cable.

Ensure the DIP switches are configured for the correct baud rate. For US2000/US3000/US5000, **all DIP switches OFF** selects the default baud rate of **115200**.

![DIP Switches](img/dip-switches.jpg)
*Pylontech US2000 with all DIP switches OFF*

## Upgrading from v1.x

Version 2.0 changed the architecture: direct serial/TCP connection from HA was replaced by the MQTT sidecar model.

**Existing config entries cannot be migrated automatically.** After upgrading:

1. Delete the old integration entry in **Settings** > **Devices & Services**.
2. Deploy the sidecar container (Step 1 above).
3. Re-add the integration pointing to your MQTT broker (Steps 2–3 above).

## Troubleshooting

- **Entities stay unavailable**: Check that the sidecar container is running (`docker compose logs -f pylon2mqtt`) and that `MQTT_TOPIC_PREFIX` matches in both the container and the HA integration.
- **Sidecar can't connect to BMS**: Verify the serial port path or TCP host/port. For serial mode, confirm the `devices:` mapping in the compose file includes the correct device node.
- **Serial permissions (HA Core/Docker)**: Ensure the USB device is passed through to the sidecar container via the `devices:` key in `docker-compose.yml`.
- **Wrong data**: Check that you are connected to the **Console** port of the master battery, not the CAN or RS485 port.
