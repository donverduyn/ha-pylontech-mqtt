import hashlib
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PylontechCoordinator


def discover_new_ids(
    items: list[dict[str, Any]], id_key: str, seen_ids: set[int]
) -> list[int]:
    """Return id_key values in items not yet in seen_ids, adding them to it.

    Shared by sensor.py and number.py's async_setup_entry: both dynamically
    add entities as new battery/cell ids show up in successive MQTT payloads,
    rather than all at once, since module/cell counts aren't known upfront.
    """
    new_ids: list[int] = []
    for item in items:
        item_id = item.get(id_key)
        if item_id is None or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        new_ids.append(item_id)
    return new_ids


def stack_id_from_topic(topic_prefix: str) -> str:
    """Derive a registry-safe identity token from a topic prefix alone.

    Superseded by stack_id_from_broker(), which also folds in host/port so
    two brokers sharing the default topic don't collide. Kept only so
    __init__._migrate_registry_identity() can still recognize and rewrite
    identities created by that older, topic-only scheme.
    """
    return topic_prefix.replace("/", "_")


def stack_id_from_broker(host: str, port: int, topic_prefix: str) -> str:
    """Derive a registry-safe, collision-resistant identity token for a stack.

    Hashing host+port+topic (rather than string-munging the topic alone)
    fixes two collision cases a plain "/" -> "_" replace can't avoid: two
    brokers that both use the default topic, and distinct topics like
    "plant/stack" and "plant_stack" that would otherwise map to the same
    token. The entry ID is deliberately excluded — it's a fresh random UUID
    every time a config entry is created, so basing identity on it means
    deleting and re-adding the integration orphans every entity, device,
    customization, and dashboard reference. Host+port+topic is what a user
    re-enters identically across such a reinstall, so identity survives it.
    """
    digest = hashlib.sha256(f"{host}\x00{port}\x00{topic_prefix}".encode()).hexdigest()
    return digest[:16]


class PylontechSystemEntity(CoordinatorEntity[PylontechCoordinator]):
    """Base class for Pylontech System entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PylontechCoordinator, stack_id: str):
        super().__init__(coordinator)
        self._stack_id = stack_id

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

    def __init__(self, coordinator: PylontechCoordinator, stack_id: str, bat_id: int):
        super().__init__(coordinator)
        self._stack_id = stack_id
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
    """Base class for per-cell entities, attached to the parent battery module."""

    def __init__(
        self,
        coordinator: PylontechCoordinator,
        stack_id: str,
        bat_id: int,
        cell_id: int,
    ):
        super().__init__(coordinator, stack_id, bat_id)
        self._cell_id = cell_id

    @property
    def available(self) -> bool:
        """Also unavailable when this specific cell drops out of the
        module's cell list, even if the module itself is still present."""
        return super().available and self.coordinator.is_cell_present(
            self._bat_id, self._cell_id
        )
