"""Constants for the Pylontech Serial integration."""

DOMAIN = "pylontech_serial"

CONF_SERIAL_PORT = "serial_port"
CONF_BAUD_RATE = "baud_rate"
CONF_POLL_INTERVAL = "poll_interval"

CONF_CONNECTION_TYPE = "connection_type"
CONF_TCP_HOST = "tcp_host"
CONF_TCP_PORT = "tcp_port"

CONNECTION_TYPE_SERIAL = "serial"
CONNECTION_TYPE_TCP = "tcp"

DEFAULT_BAUD_RATE = 115200
DEFAULT_POLL_INTERVAL = 15  # seconds
DEFAULT_TCP_PORT = 23  # standard Telnet/serial-over-TCP port
CONF_BATTERY_CAPACITY = "battery_capacity"
DEFAULT_BATTERY_CAPACITY = 2.4  # kWh — US2000 (50 Ah @ 48 V)
BATTERY_CAPACITY_US2000  = 2.4  # kWh
BATTERY_CAPACITY_US3000  = 3.5  # kWh (74 Ah @ 48 V)
BATTERY_CAPACITY_US5000  = 4.8  # kWh (100 Ah @ 48 V)
