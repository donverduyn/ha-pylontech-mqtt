from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PylontechCoordinator


class PylontechSystemEntity(CoordinatorEntity[PylontechCoordinator]):
    """Base class for Pylontech System entities."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        data = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, "system")},
            name="Pylontech Stack",
            manufacturer=(data.get("manufacturer") or "Pylontech")
            if data
            else "Pylontech",
            model=data.get("model") if data else None,
            sw_version=data.get("fw_version") if data else None,
            serial_number=data.get("barcode") if data else None,
        )


class PylontechBatteryEntity(CoordinatorEntity[PylontechCoordinator]):
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
            manufacturer=(data.get("manufacturer") or "Pylontech")
            if data
            else "Pylontech",
            model=data.get("model") if data else None,
            sw_version=data.get("fw_version") if data else None,
            via_device=(DOMAIN, "system"),
        )


class PylontechCellEntity(PylontechBatteryEntity):
    """Base class for per-cell entities, attached to the parent battery module device."""

    def __init__(self, coordinator, bat_id: int, cell_id: int):
        super().__init__(coordinator, bat_id)
        self._cell_id = cell_id
