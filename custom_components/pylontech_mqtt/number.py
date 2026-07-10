"""Number platform for Pylontech MQTT."""

from homeassistant.components.number import NumberDeviceClass, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import PylontechCoordinator
from .entity import PylontechBatteryEntity, discover_new_ids


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the number platform."""
    coordinator: PylontechCoordinator = hass.data[DOMAIN][entry.entry_id]

    seen_bat_ids: set[int] = set()

    def _add_new_batteries() -> None:
        if not coordinator.data:
            return
        new_entities = [
            PylontechBatteryCapacityNumber(coordinator, coordinator.stack_id, bat_id)
            for bat_id in discover_new_ids(
                coordinator.data.get("batteries", []), "sys_id", seen_bat_ids
            )
        ]
        if new_entities:
            async_add_entities(new_entities)

    _add_new_batteries()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_batteries))


class PylontechBatteryCapacityNumber(PylontechBatteryEntity, RestoreNumber):
    """Representation of a Per-Battery Capacity Number."""

    def __init__(
        self,
        coordinator: PylontechCoordinator,
        stack_id: str,
        bat_id: int,
    ) -> None:
        super().__init__(coordinator, stack_id, bat_id)

        self._attr_unique_id = f"{self._stack_id}_bat{bat_id}_capacity"
        self._attr_translation_key = "battery_capacity"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = NumberDeviceClass.ENERGY_STORAGE
        self._attr_entity_category = EntityCategory.CONFIG

        self._attr_native_min_value = 0.5
        self._attr_native_max_value = 20.0
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

        # Clamp to current min/max in case a previously persisted value falls
        # outside the now-allowed range (e.g. saved under an older version).
        capacity = max(
            self._attr_native_min_value, min(self._attr_native_max_value, capacity)
        )
        self._attr_native_value = capacity
        self.coordinator.set_battery_capacity(self._bat_id, capacity)
        # Re-compute energy_stored with the restored capacity immediately so
        # the sensor reflects the correct value without waiting for the next
        # MQTT push (typically 15 s).
        # See coordinator.py's _async_update_data: HA's own DataUpdateCoordinator
        # types .data as _DataT (never None) even though it starts out None,
        # so this real runtime check reads as always-true to pyright.
        if self.coordinator.data is not None:  # pyright: ignore[reportUnnecessaryComparison]
            await self.coordinator.async_request_refresh()

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        self._attr_native_value = value
        self.coordinator.set_battery_capacity(self._bat_id, value)
        self.async_write_ha_state()

        # Trigger an update to recompute energy stored with new capacity immediately
        await self.coordinator.async_request_refresh()
