"""Sensor platform for Pylontech Serial."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricPotential,
    UnitOfElectricCurrent,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfEnergy,
    PERCENTAGE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .entity import PylontechSystemEntity, PylontechBatteryEntity

from .const import DOMAIN
# from .structs import PylontechSystem, PylontechBattery # Not strictly needed at runtime if we don't type hint heavily, but good for ref.

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    unique_id_prefix = entry.entry_id
    entities = []
    
    # --- System Sensors ---
    # Voltage
    entities.append(PylontechSystemSensor(
        coordinator, unique_id_prefix, "sys_volt", 
        UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, "voltage",
        state_class=SensorStateClass.MEASUREMENT
    ))
    # Current
    entities.append(PylontechSystemSensor(
        coordinator, unique_id_prefix, "sys_curr", 
        UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, "current",
        state_class=SensorStateClass.MEASUREMENT
    ))
    # SOC
    entities.append(PylontechSystemSensor(
        coordinator, unique_id_prefix, "sys_soc", 
        PERCENTAGE, SensorDeviceClass.BATTERY, "soc",
        state_class=SensorStateClass.MEASUREMENT
    ))
    # Power
    entities.append(PylontechSystemSensor(
        coordinator, unique_id_prefix, "sys_power", 
        UnitOfPower.WATT, SensorDeviceClass.POWER, "power",
        state_class=SensorStateClass.MEASUREMENT
    ))
    # Energy In
    entities.append(PylontechSystemSensor(
        coordinator, unique_id_prefix, "sys_energy_in", 
        UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, "energy_in",
        state_class=SensorStateClass.TOTAL_INCREASING
    ))
    # Energy Out
    entities.append(PylontechSystemSensor(
        coordinator, unique_id_prefix, "sys_energy_out", 
        UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY, "energy_out",
        state_class=SensorStateClass.TOTAL_INCREASING
    ))
    # Stored Energy
    entities.append(PylontechSystemSensor(
        coordinator, unique_id_prefix, "sys_energy_stored", 
        UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY_STORAGE, "energy_stored",
        state_class=SensorStateClass.MEASUREMENT
    ))
    # SOH (System)
    entities.append(PylontechSystemSensor(
        coordinator, unique_id_prefix, "sys_soh", 
        PERCENTAGE, SensorDeviceClass.BATTERY, "soh",
        state_class=SensorStateClass.MEASUREMENT
    ))
    # Cycles (System)
    entities.append(PylontechSystemSensor(
        coordinator, unique_id_prefix, "sys_cycles", 
        None, None, "cycles",
        state_class=SensorStateClass.MEASUREMENT
    ))
    # Raw Data (System)
    entities.append(PylontechSystemSensor(
        coordinator, unique_id_prefix, "sys_raw", 
        None, None, "raw",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False
    ))

    # Info Sensors (Diagnostic)
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_cell_count",  None, None, "cell_count",  entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_fw_version",  None, None, "fw_version",  entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_spec",         None, None, "spec",         entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_barcode",      None, None, "barcode",      entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_bms_time",     None, None, "bms_time",     entity_category=EntityCategory.DIAGNOSTIC))

    # Info — additional version / limit fields
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_board_version", None, None, "board_version", entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_soft_version",  None, None, "soft_version",  entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_boot_version",  None, None, "boot_version",  entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_comm_version",  None, None, "comm_version",  entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_release_date",  None, None, "release_date",  entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_max_charge_curr", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, "max_charge_curr", state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_max_dischg_curr", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, "max_dischg_curr", state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))

    # Stat — usage counters
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_charge_times",  None, None, "charge_times",  state_class=SensorStateClass.MEASUREMENT))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_discharge_cnt", None, None, "discharge_cnt", state_class=SensorStateClass.MEASUREMENT))

    # Stat — fault / event counters (diagnostic)
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_sc_times",         None, None, "sc_times",         state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_bat_ov_times",     None, None, "bat_ov_times",     state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_bat_hv_times",     None, None, "bat_hv_times",     state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_bat_lv_times",     None, None, "bat_lv_times",     state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_bat_uv_times",     None, None, "bat_uv_times",     state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_pwr_ov_times",     None, None, "pwr_ov_times",     state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_pwr_hv_times",     None, None, "pwr_hv_times",     state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_shut_times",       None, None, "shut_times",       state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_reset_times",      None, None, "reset_times",      state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_life_warn_times",  None, None, "life_warn_times",  state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_life_alarm_times", None, None, "life_alarm_times", state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_pwr_coulomb",      None, None, "pwr_coulomb",      state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))
    entities.append(PylontechSystemSensor(coordinator, unique_id_prefix, "sys_dsg_cap",          None, None, "dsg_cap",          state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC))


    # System sensors are stable — register them immediately.
    async_add_entities(entities)

    # --- Per-Battery Sensors ---
    # Created dynamically so batteries discovered after startup (e.g. BMS
    # temporarily unavailable at boot) are picked up without a reload.
    seen_bat_ids: set[int] = set()

    def _make_battery_sensors(bat_id: int) -> list:
        return [
            # Standard sensors
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "volt",         UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE,      "voltage",      state_class=SensorStateClass.MEASUREMENT),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "curr",         UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT,      "current",      state_class=SensorStateClass.MEASUREMENT),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "temp",         UnitOfTemperature.CELSIUS,    SensorDeviceClass.TEMPERATURE,  "temperature",  state_class=SensorStateClass.MEASUREMENT),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "soc",          PERCENTAGE,                   SensorDeviceClass.BATTERY,      "soc",          state_class=SensorStateClass.MEASUREMENT),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "power",        UnitOfPower.WATT,             SensorDeviceClass.POWER,        "power",        state_class=SensorStateClass.MEASUREMENT),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "energy_stored",UnitOfEnergy.KILO_WATT_HOUR,  SensorDeviceClass.ENERGY_STORAGE,"energy_stored",state_class=SensorStateClass.MEASUREMENT),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "status",       None, None, "status"),
            # Cell-level min/max (Vlow/Vhigh/Tlow/Thigh from pwr table)
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "volt_low",     UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE,      "volt_low",     state_class=SensorStateClass.MEASUREMENT),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "volt_high",    UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE,      "volt_high",    state_class=SensorStateClass.MEASUREMENT),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "temp_low",     UnitOfTemperature.CELSIUS,    SensorDeviceClass.TEMPERATURE,  "temp_low",     state_class=SensorStateClass.MEASUREMENT),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "temp_high",    UnitOfTemperature.CELSIUS,    SensorDeviceClass.TEMPERATURE,  "temp_high",    state_class=SensorStateClass.MEASUREMENT),
            # Status strings (diagnostic)
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "volt_status",  None, None, "volt_status",  entity_category=EntityCategory.DIAGNOSTIC),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "curr_status",  None, None, "curr_status",  entity_category=EntityCategory.DIAGNOSTIC),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "temp_status",  None, None, "temp_status",  entity_category=EntityCategory.DIAGNOSTIC),
            # Battery-level state columns (B.V.St / B.T.St from pwr table)
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "bvst",         None, None, "batt_volt_status", entity_category=EntityCategory.DIAGNOSTIC),
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "btst",         None, None, "batt_temp_status", entity_category=EntityCategory.DIAGNOSTIC),
            # Raw line (diagnostic, disabled by default)
            PylontechBatterySensor(coordinator, unique_id_prefix, bat_id, "raw",          None, None, "raw",          entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False),
        ]

    def _add_new_batteries() -> None:
        if not coordinator.data:
            return
        new_entities = []
        for bat in coordinator.data.batteries:
            if bat.sys_id not in seen_bat_ids:
                seen_bat_ids.add(bat.sys_id)
                new_entities.extend(_make_battery_sensors(bat.sys_id))
        if new_entities:
            async_add_entities(new_entities)

    _add_new_batteries()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_batteries))


class PylontechSystemSensor(PylontechSystemEntity, SensorEntity):
    """Representation of a System-wide Sensor."""

    def __init__(self, coordinator, unique_id_prefix, key, unit, device_class, attr_name, state_class=None, entity_category=None, entity_registry_enabled_default=True):
        super().__init__(coordinator)
        self._attribute_key = attr_name # field name in struct
        self._unit = unit
        self._device_class = device_class
        self._attr_state_class = state_class
        self._attr_entity_category = entity_category
        self._attr_entity_registry_enabled_default = entity_registry_enabled_default
        
        self._attr_unique_id = f"{unique_id_prefix}_{key}"
        self._attr_translation_key = key

    @property
    def native_value(self):
        if not self.coordinator.data: return None
        return getattr(self.coordinator.data, self._attribute_key, None)

    @property
    def native_unit_of_measurement(self):
        return self._unit

    @property
    def device_class(self):
        return self._device_class
    
    @property
    def extra_state_attributes(self):
        return {}


class PylontechBatterySensor(PylontechBatteryEntity, SensorEntity):
    """Representation of a Per-Battery Sensor."""

    def __init__(self, coordinator, unique_id_prefix, bat_id, suffix, unit, device_class, attr_name, entity_category=None, entity_registry_enabled_default=True, state_class=None):
        super().__init__(coordinator, bat_id)
        self._attribute_key = attr_name
        self._unit = unit
        self._device_class = device_class
        self._attr_entity_category = entity_category
        self._attr_entity_registry_enabled_default = entity_registry_enabled_default
        self._attr_state_class = state_class

        self._attr_unique_id = f"{unique_id_prefix}_bat{bat_id}_{suffix}"
        self._attr_translation_key = f"bat_{suffix}"

    @property
    def native_value(self):
        if not self.coordinator.data: return None
        for b in self.coordinator.data.batteries:
            if b.sys_id == self._bat_id:
                return getattr(b, self._attribute_key, None)
        return None

    @property
    def native_unit_of_measurement(self):
        return self._unit

    @property
    def device_class(self):
        return self._device_class
