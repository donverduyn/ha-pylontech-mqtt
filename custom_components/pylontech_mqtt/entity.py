from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PylontechCoordinator


def stack_id_from_topic(topic_prefix: str) -> str:
    """Derive a registry-safe identity token from the sidecar's MQTT topic prefix.

    The topic prefix (not the config-entry ID) is used as the basis for every
    unique_id and device identifier in this integration. The entry ID is a
    fresh random UUID every time an entry is created, so basing identity on
    it means deleting and re-adding the integration — the documented upgrade
    path for breaking schema changes — orphans every entity, device,
    customization, and dashboard reference. The topic prefix is the one
    piece of configuration a user re-enters identically across such a
    reinstall, so identity survives it.
    """
    return topic_prefix.replace("/", "_")


class PylontechSystemEntity(CoordinatorEntity[PylontechCoordinator]):
    """Base class for Pylontech System entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, topic_prefix: str):
        super().__init__(coordinator)
        self._stack_id = stack_id_from_topic(topic_prefix)

    @property
    def device_info(self) -> DeviceInfo:
        data = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._stack_id}_system")},
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

    def __init__(self, coordinator, topic_prefix: str, bat_id: int):
        super().__init__(coordinator)
        self._stack_id = stack_id_from_topic(topic_prefix)
        self._bat_id = bat_id

    @property
    def available(self) -> bool:
        """Unavailable when the coordinator is down, or this module has
        dropped out of the stack's most recent MQTT payload (see
        PylontechCoordinator.is_battery_present). Without this, a module
        that goes missing would keep reporting its last known values
        forever with no indication anything is wrong.
        """
        return super().available and self.coordinator.is_battery_present(self._bat_id)

    @property
    def device_info(self) -> DeviceInfo:
        data = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._stack_id}_battery_{self._bat_id}")},
            name=f"Pylontech Module {self._bat_id}",
            manufacturer=(data.get("manufacturer") or "Pylontech")
            if data
            else "Pylontech",
            model=data.get("model") if data else None,
            sw_version=data.get("fw_version") if data else None,
            via_device=(DOMAIN, f"{self._stack_id}_system"),
        )


class PylontechCellEntity(PylontechBatteryEntity):
    """Base class for per-cell entities, attached to the parent battery module device."""

    def __init__(self, coordinator, topic_prefix: str, bat_id: int, cell_id: int):
        super().__init__(coordinator, topic_prefix, bat_id)
        self._cell_id = cell_id

    @property
    def available(self) -> bool:
        """Also unavailable when this specific cell drops out of the
        module's cell list, even if the module itself is still present."""
        return super().available and self.coordinator.is_cell_present(
            self._bat_id, self._cell_id
        )
