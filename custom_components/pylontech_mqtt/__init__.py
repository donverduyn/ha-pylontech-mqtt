"""The Pylontech MQTT integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_MQTT_HOST,
    CONF_MQTT_PASS,
    CONF_MQTT_PORT,
    CONF_MQTT_TOPIC,
    CONF_MQTT_USER,
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

    coordinator = PylontechCoordinator(
        hass=hass,
        mqtt_host=_get(CONF_MQTT_HOST, ""),
        mqtt_port=_get(CONF_MQTT_PORT, DEFAULT_MQTT_PORT),
        mqtt_user=_get(CONF_MQTT_USER, ""),
        mqtt_pass=_get(CONF_MQTT_PASS, ""),
        topic_prefix=_get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC),
    )

    # connect_async + loop_start are non-blocking; no executor needed.
    coordinator.setup()

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
    # Always remove and shut down the coordinator so the MQTT client thread is
    # never leaked, even when platform unload partially fails.
    coordinator: PylontechCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await hass.async_add_executor_job(coordinator.shutdown)
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry to the current schema version.

    Currently a no-op placeholder — ConfigFlow.VERSION is still 1.
    When future schema changes are introduced, migration logic goes here.
    """
    _LOGGER.debug(
        "Migrating config entry from version %s.%s",
        entry.version,
        entry.minor_version,
    )
    return True
