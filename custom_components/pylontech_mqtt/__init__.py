"""The Pylontech MQTT integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_MQTT_HOST,
    CONF_MQTT_PASS,
    CONF_MQTT_PORT,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USER,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
)
from .coordinator import PylontechCoordinator

PLATFORMS = ["sensor", "number"]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Pylontech from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    def _get(key, default=None):
        return entry.options.get(key, entry.data.get(key, default))

    # Guard against old serial/TCP config entries that pre-date the MQTT refactor.
    if not _get(CONF_MQTT_HOST):
        _LOGGER.error(
            "Pylontech config entry is missing MQTT settings (schema changed in v2.0). "
            "Please delete and re-add the integration."
        )
        return False

    def _get(key, default=None):
        return entry.options.get(key, entry.data.get(key, default))

    coordinator = PylontechCoordinator(
        hass=hass,
        mqtt_host=_get(CONF_MQTT_HOST, ""),
        mqtt_port=_get(CONF_MQTT_PORT, DEFAULT_MQTT_PORT),
        mqtt_user=_get(CONF_MQTT_USER, ""),
        mqtt_pass=_get(CONF_MQTT_PASS, ""),
        topic_prefix=_get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC),
        default_capacity=_get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY),
    )

    # Connect to MQTT broker in a thread (paho is blocking).
    await hass.async_add_executor_job(coordinator.setup)

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: PylontechCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await hass.async_add_executor_job(coordinator.shutdown)
    return unload_ok
