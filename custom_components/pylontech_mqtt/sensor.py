"""Sensor platform for Pylontech MQTT."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PylontechBatteryEntity, PylontechCellEntity, PylontechSystemEntity

# ---------------------------------------------------------------------------
# Descriptor tables — one row per sensor, no entity subclass per sensor needed.
# `key`             — attribute name on PylontechSystem / PylontechBattery.
# `translation_key` — maps to entity.sensor.<key>.name in the translations file.
# ---------------------------------------------------------------------------

SYSTEM_SENSORS: tuple[SensorEntityDescription, ...] = (
    # --- Live measurements ---
    SensorEntityDescription(
        key="voltage",
        translation_key="sys_volt",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="current",
        translation_key="sys_curr",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="soc",
        translation_key="sys_soc",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="power",
        translation_key="sys_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # --- Energy ---
    SensorEntityDescription(
        key="energy_in",
        translation_key="sys_energy_in",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
    ),
    SensorEntityDescription(
        key="energy_out",
        translation_key="sys_energy_out",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
    ),
    SensorEntityDescription(
        key="energy_stored",
        translation_key="sys_energy_stored",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # --- Health ---
    SensorEntityDescription(
        key="soh",
        translation_key="sys_soh",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="cycles",
        translation_key="sys_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    # --- Usage counters ---
    SensorEntityDescription(
        key="charge_times",
        translation_key="sys_charge_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="discharge_cnt",
        translation_key="sys_discharge_cnt",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    SensorEntityDescription(
        key="idle_times",
        translation_key="sys_idle_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    # --- Device info (diagnostic) ---
    SensorEntityDescription(
        key="cell_count",
        translation_key="sys_cell_count",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="fw_version",
        translation_key="sys_fw_version",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="spec",
        translation_key="sys_spec",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="barcode",
        translation_key="sys_barcode",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="bms_time",
        translation_key="sys_bms_time",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="board_version",
        translation_key="sys_board_version",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="soft_version",
        translation_key="sys_soft_version",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="boot_version",
        translation_key="sys_boot_version",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="comm_version",
        translation_key="sys_comm_version",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="release_date",
        translation_key="sys_release_date",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="max_charge_curr",
        translation_key="sys_max_charge_curr",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="max_dischg_curr",
        translation_key="sys_max_dischg_curr",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- Fault / event counters (diagnostic) ---
    SensorEntityDescription(
        key="sc_times",
        translation_key="sys_sc_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="bat_ov_times",
        translation_key="sys_bat_ov_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="bat_hv_times",
        translation_key="sys_bat_hv_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="bat_lv_times",
        translation_key="sys_bat_lv_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="bat_uv_times",
        translation_key="sys_bat_uv_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="pwr_ov_times",
        translation_key="sys_pwr_ov_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="pwr_hv_times",
        translation_key="sys_pwr_hv_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="shut_times",
        translation_key="sys_shut_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="reset_times",
        translation_key="sys_reset_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="life_warn_times",
        translation_key="sys_life_warn_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="life_alarm_times",
        translation_key="sys_life_alarm_times",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="pwr_coulomb",
        translation_key="sys_pwr_coulomb",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="dsg_cap",
        translation_key="sys_dsg_cap",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- Raw response (hidden by default) ---
    SensorEntityDescription(
        key="raw",
        translation_key="sys_raw",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)

BATTERY_SENSORS: tuple[SensorEntityDescription, ...] = (
    # --- Live measurements ---
    SensorEntityDescription(
        key="voltage",
        translation_key="bat_volt",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="current",
        translation_key="bat_curr",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="temperature",
        translation_key="bat_temp",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="soc",
        translation_key="bat_soc",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="power",
        translation_key="bat_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="energy_stored",
        translation_key="bat_energy_stored",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="status",
        translation_key="bat_status",
    ),
    # --- Cell extremes (diagnostic) ---
    SensorEntityDescription(
        key="temp_low",
        translation_key="bat_temp_low",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="temp_high",
        translation_key="bat_temp_high",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="volt_low",
        translation_key="bat_volt_low",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="volt_high",
        translation_key="bat_volt_high",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- Protection status strings (diagnostic) ---
    SensorEntityDescription(
        key="volt_status",
        translation_key="bat_volt_status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="curr_status",
        translation_key="bat_curr_status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="temp_status",
        translation_key="bat_temp_status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="batt_volt_status",
        translation_key="bat_bvst",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="batt_temp_status",
        translation_key="bat_btst",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- Raw response (hidden by default) ---
    SensorEntityDescription(
        key="raw",
        translation_key="bat_raw",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)

# ---------------------------------------------------------------------------
# Cell-level sensors — one row per measurement, names set dynamically to
# include the cell index (e.g. "Cell 0 Voltage").
# No translation_key; _attr_name is set per-instance in PylontechCellSensor.
# ---------------------------------------------------------------------------

CELL_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="voltage",
        name="Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="current",
        name="Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="temperature",
        name="Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="soc",
        name="SOC",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="base_state",
        name="State",
    ),
    SensorEntityDescription(
        key="volt_status",
        name="Voltage Status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="curr_status",
        name="Current Status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="temp_status",
        name="Temperature Status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="capacity",
        name="Capacity",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities for the Pylontech MQTT integration."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # System-level sensors are always known upfront.
    async_add_entities(
        PylontechSystemSensor(coordinator, entry.entry_id, desc)
        for desc in SYSTEM_SENSORS
    )

    # Per-battery sensors are added dynamically: the number of modules is not
    # known until the first MQTT message arrives from the sidecar.
    seen_bat_ids: set[int] = set()

    def _add_new_batteries() -> None:
        if not coordinator.data:
            return
        new_entities: list[PylontechBatterySensor] = []
        for bat in coordinator.data.batteries:
            if bat.sys_id not in seen_bat_ids:
                seen_bat_ids.add(bat.sys_id)
                new_entities.extend(
                    PylontechBatterySensor(
                        coordinator, entry.entry_id, bat.sys_id, desc
                    )
                    for desc in BATTERY_SENSORS
                )
        if new_entities:
            async_add_entities(new_entities)

    # Per-cell sensors are added dynamically: cell count is not known until the
    # first MQTT message that includes cell-level data from the sidecar's bat N
    # commands.
    seen_cell_ids: dict[int, set[int]] = {}  # bat_id → set of seen cell_ids

    def _add_new_cells() -> None:
        if not coordinator.data:
            return
        new_entities: list[PylontechCellSensor] = []
        for bat in coordinator.data.batteries:
            bat_cells = seen_cell_ids.setdefault(bat.sys_id, set())
            for cell in bat.cells:
                if cell.cell_id not in bat_cells:
                    bat_cells.add(cell.cell_id)
                    new_entities.extend(
                        PylontechCellSensor(
                            coordinator, entry.entry_id, bat.sys_id, cell.cell_id, desc
                        )
                        for desc in CELL_SENSORS
                    )
        if new_entities:
            async_add_entities(new_entities)

    _add_new_batteries()
    _add_new_cells()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_batteries))
    entry.async_on_unload(coordinator.async_add_listener(_add_new_cells))


# ---------------------------------------------------------------------------
# Entity classes — one generic class per device tier
# ---------------------------------------------------------------------------


class PylontechSystemSensor(PylontechSystemEntity, SensorEntity):
    """Reads a single attribute from the system-level PylontechSystem object."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator,
        entry_id: str,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return getattr(self.coordinator.data, self.entity_description.key, None)


class PylontechBatterySensor(PylontechBatteryEntity, SensorEntity):
    """Reads a single attribute from a per-module PylontechBattery object."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator,
        entry_id: str,
        bat_id: int,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, bat_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_bat{bat_id}_{description.key}"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        for bat in self.coordinator.data.batteries:
            if bat.sys_id == self._bat_id:
                return getattr(bat, self.entity_description.key, None)
        return None


class PylontechCellSensor(PylontechCellEntity, SensorEntity):
    """Reads a single attribute from a per-cell PylontechCell object."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator,
        entry_id: str,
        bat_id: int,
        cell_id: int,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, bat_id, cell_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_bat{bat_id}_cell{cell_id}_{description.key}"
        self._attr_name = f"Cell {cell_id} {description.name}"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        for bat in self.coordinator.data.batteries:
            if bat.sys_id == self._bat_id:
                for cell in bat.cells:
                    if cell.cell_id == self._cell_id:
                        return getattr(cell, self.entity_description.key, None)
        return None
