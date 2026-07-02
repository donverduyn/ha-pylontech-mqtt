# Contributing to Pylontech MQTT Integration

Thank you for your interest in contributing! This guide covers adding translations and general development workflow.

## Architecture Overview

This integration works in two parts:

1. **Docker sidecar** (`docker/pylon2mqtt.py`) — runs alongside Home Assistant, connects to the Pylontech BMS over serial or TCP, and publishes parsed data to an MQTT broker.
2. **Home Assistant integration** (`custom_components/pylontech_mqtt/`) — subscribes to the MQTT broker and exposes the data as HA entities.

The parsing logic (`parser.py`, `structs.py`) lives inside the integration and is copied into the Docker image at build time, ensuring both sides always use identical logic.

## Adding Translations

We welcome translations to make this integration accessible to everyone!

1. **Locate the Translations**: Go to `custom_components/pylontech_mqtt/translations/`.
2. **Create your Language File**:
    - Find the English file: `en.json`.
    - Copy it and name the new file with your language's ISO 639-1 code (e.g., `es.json` for Spanish, `fr.json` for French, `de.json` for German).
3. **Translate**:
    - Open your new file (e.g., `es.json`).
    - Translate the values on the right side of the colon. **Do not change the keys** (the text on the left).

   **Example (`es.json`):**
   ```json
   {
     "config": {
       "step": {
         "user": {
           "data": {
             "mqtt_host": "Dirección del Broker"
           }
         }
       }
     }
   }
   ```

## Submitting a Pull Request

1. Create a Pull Request with your changes.
2. If adding a translation, mention the language you are adding.
