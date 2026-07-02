"""Number platform for Pylontech MQTT."""

from homeassistant.components.number import NumberDeviceClass, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PylontechBatteryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the number platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    unique_id_prefix = entry.entry_id
    entities = []

    async_add_entities(entities)

    seen_bat_ids: set[int] = set()

    def _add_new_batteries() -> None:
        if not coordinator.data:
            return
        new_entities = []
        for bat in coordinator.data.batteries:
            if bat.sys_id not in seen_bat_ids:
                seen_bat_ids.add(bat.sys_id)
                new_entities.append(PylontechBatteryCapacityNumber(coordinator, unique_id_prefix, bat.sys_id))
        if new_entities:
            async_add_entities(new_entities)

    _add_new_batteries()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_batteries))


class PylontechBatteryCapacityNumber(PylontechBatteryEntity, RestoreNumber):
    """Representation of a Per-Battery Capacity Number."""

    def __init__(self, coordinator, unique_id_prefix, bat_id):
        super().__init__(coordinator, bat_id)

        self._attr_unique_id = f"{unique_id_prefix}_bat{bat_id}_capacity"
        self._attr_translation_key = "battery_capacity"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = NumberDeviceClass.ENERGY_STORAGE
        self._attr_entity_category = EntityCategory.CONFIG

        self._attr_native_min_value = 0.5
        self._attr_native_max_value = 10.0
        self._attr_native_step = 0.1
        self._attr_mode = NumberMode.BOX

        self._attr_native_value = coordinator.default_capacity

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        last_number_data = await self.async_get_last_number_data()
        if last_number_data is not None and last_number_data.native_value is not None:
            capacity: float = float(last_number_data.native_value)
        else:
            capacity = float(self.coordinator.default_capacity)

        self._attr_native_value = capacity
        self.coordinator.set_battery_capacity(self._bat_id, capacity)

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        self._attr_native_value = value
        self.coordinator.set_battery_capacity(self._bat_id, value)
        self.async_write_ha_state()

        # Trigger an update to recompute energy stored with new capacity immediately
        await self.coordinator.async_request_refresh()
