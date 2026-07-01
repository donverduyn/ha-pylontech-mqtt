from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

class PylontechSystemEntity(CoordinatorEntity):
    """Base class for Pylontech System entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator):
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        data = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, "system")},
            name="Pylontech Stack",
            manufacturer=(data.manufacturer or "Pylontech") if data else "Pylontech",
            model=data.model        if data else None,
            sw_version=data.fw_version  if data else None,
            serial_number=data.barcode  if data else None,
        )


class PylontechBatteryEntity(CoordinatorEntity):
    """Base class for Pylontech per-battery entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, bat_id: int):
        super().__init__(coordinator)
        self._bat_id = bat_id

    @property
    def device_info(self) -> DeviceInfo:
        data = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, f"battery_{self._bat_id}")},
            name=f"Pylontech Module {self._bat_id}",
            manufacturer=(data.manufacturer or "Pylontech") if data else "Pylontech",
            model=data.model        if data else None,
            sw_version=data.fw_version  if data else None,
            via_device=(DOMAIN, "system"),
        )
