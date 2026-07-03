"""The Pylontech MQTT integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

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
from .entity import stack_id_from_topic

PLATFORMS = ["sensor", "number"]

_LOGGER = logging.getLogger(__name__)


def _migrate_registry_identity(
    hass: HomeAssistant, entry: ConfigEntry, topic_prefix: str
) -> None:
    """Rename registry entries from the legacy entry-id-based identity scheme
    to the stable topic-based one (see entity.stack_id_from_topic).

    Entity/device identity used to be built from entry.entry_id — a fresh
    random value every time a config entry is created — so the documented
    delete-and-recreate upgrade path silently orphaned every entity, device,
    customization, and dashboard reference. This runs on every setup; it is
    a plain string-prefix rename, so it is a no-op once an entry's registry
    entries already use the new scheme.
    """
    old_prefix = f"{entry.entry_id}_"
    new_prefix = f"{stack_id_from_topic(topic_prefix)}_"
    if old_prefix == new_prefix:
        return

    device_reg = dr.async_get(hass)
    for device in list(dr.async_entries_for_config_entry(device_reg, entry.entry_id)):
        new_identifiers = set()
        changed = False
        for domain, ident in device.identifiers:
            if domain == DOMAIN and ident.startswith(old_prefix):
                new_identifiers.add((domain, new_prefix + ident[len(old_prefix) :]))
                changed = True
            else:
                new_identifiers.add((domain, ident))
        if changed:
            device_reg.async_update_device(device.id, new_identifiers=new_identifiers)

    entity_reg = er.async_get(hass)
    for entity in list(er.async_entries_for_config_entry(entity_reg, entry.entry_id)):
        if entity.unique_id and entity.unique_id.startswith(old_prefix):
            new_unique_id = new_prefix + entity.unique_id[len(old_prefix) :]
            entity_reg.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Pylontech from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    def _get(key, default=None):
        return entry.options.get(key, entry.data.get(key, default))

    # Guard against old serial/TCP config entries that pre-date the MQTT refactor.
    if not _get(CONF_MQTT_HOST):
        _LOGGER.error(
            "Pylontech config entry is missing MQTT settings. "
            "Please delete and re-add the integration."
        )
        return False

    topic_prefix = _get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC)
    _migrate_registry_identity(hass, entry, topic_prefix)

    coordinator = PylontechCoordinator(
        hass=hass,
        mqtt_host=_get(CONF_MQTT_HOST, ""),
        mqtt_port=_get(CONF_MQTT_PORT, DEFAULT_MQTT_PORT),
        mqtt_user=_get(CONF_MQTT_USER, ""),
        mqtt_pass=_get(CONF_MQTT_PASS, ""),
        topic_prefix=topic_prefix,
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
