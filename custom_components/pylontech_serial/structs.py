from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class PylontechBattery:
    sys_id: int
    voltage: float
    current: float
    temperature: float
    soc: int
    status: str
    power: float
    raw: str
    energy_stored: float

    # Extended fields available in the pwr table (None when absent / older firmware)
    temp_low: Optional[float] = None    # Tlow  – min cell temperature (°C)
    temp_high: Optional[float] = None   # Thigh – max cell temperature (°C)
    volt_low: Optional[float] = None    # Vlow  – min cell voltage (V)
    volt_high: Optional[float] = None   # Vhigh – max cell voltage (V)
    volt_status: Optional[str] = None   # Volt.St
    curr_status: Optional[str] = None   # Curr.St
    temp_status: Optional[str] = None   # Temp.St
    batt_volt_status: Optional[str] = None  # B.V.St – battery-level voltage state
    batt_temp_status: Optional[str] = None  # B.T.St – battery-level temperature state

@dataclass
class PylontechSystem:
    voltage: float
    current: float
    soc: float
    power: float
    energy_in: float
    energy_out: float
    energy_stored: float
    
    # Info Command Data
    cell_count: Optional[int] = None
    spec: Optional[str] = None
    barcode: Optional[str] = None
    fw_version: Optional[str] = None
    soft_version: Optional[str] = None
    board_version: Optional[str] = None
    boot_version: Optional[str] = None
    comm_version: Optional[str] = None
    release_date: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    max_charge_curr: Optional[float] = None  # A
    max_dischg_curr: Optional[float] = None  # A

    # Time Command Data
    bms_time: Optional[str] = None

    # Stat Command Data
    cycles: Optional[int] = None
    soh: Optional[int] = None
    charge_times: Optional[int] = None
    discharge_cnt: Optional[int] = None
    idle_times: Optional[int] = None
    shut_times: Optional[int] = None
    reset_times: Optional[int] = None
    sc_times: Optional[int] = None        # short circuit events
    bat_ov_times: Optional[int] = None    # battery overvoltage
    bat_hv_times: Optional[int] = None    # battery high voltage
    bat_lv_times: Optional[int] = None    # battery low voltage
    bat_uv_times: Optional[int] = None    # battery undervoltage
    pwr_ov_times: Optional[int] = None    # power overvoltage
    pwr_hv_times: Optional[int] = None    # power high voltage
    life_warn_times: Optional[int] = None
    life_alarm_times: Optional[int] = None
    pwr_coulomb: Optional[int] = None     # total mAh throughput
    dsg_cap: Optional[int] = None         # discharge capacity (mAh)

    raw: str = ""
    
    batteries: List[PylontechBattery] = field(default_factory=list)

    @property
    def battery_count(self) -> int:
        return len(self.batteries)
