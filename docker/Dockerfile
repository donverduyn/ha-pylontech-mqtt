FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir pyserial "paho-mqtt>=2.0.0"

# Copy shared parser & structs from the custom component (single source of truth)
COPY custom_components/pylontech_mqtt/parser.py .
COPY custom_components/pylontech_mqtt/structs.py .

# Copy the sidecar entry point
COPY docker/pylon2mqtt.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "pylon2mqtt.py"]
