FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir pyserial "paho-mqtt>=2.0.0"

# Copy parser, structs, and the sidecar entry point
COPY docker/pylon_parser.py .
COPY docker/structs.py .
COPY docker/main.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
