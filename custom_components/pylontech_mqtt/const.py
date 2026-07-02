"""Constants for the Pylontech integration."""

DOMAIN = "pylontech_mqtt"

# MQTT configuration keys
CONF_MQTT_HOST = "mqtt_host"
CONF_MQTT_PORT = "mqtt_port"
CONF_MQTT_USER = "mqtt_user"
CONF_MQTT_PASS = "mqtt_pass"
CONF_MQTT_TOPIC = "mqtt_topic"

DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_TOPIC = "pylontech/stack"

CONF_BATTERY_CAPACITY = "battery_capacity"
DEFAULT_BATTERY_CAPACITY = 2.4  # kWh — US2000 (50 Ah @ 48 V)
BATTERY_CAPACITY_US2000 = 2.4
BATTERY_CAPACITY_US3000 = 3.5
BATTERY_CAPACITY_US5000 = 4.8
