from dataclasses import dataclass, field


@dataclass
class PylontechCell:
    cell_id: int
    voltage: float  # V
    current: float  # A
    temperature: float  # °C
    base_state: str
    volt_status: str | None = None
    curr_status: str | None = None
    temp_status: str | None = None
    soc: int = 0
    capacity: int | None = None  # mAH


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
    temp_low: float | None = None  # Tlow  – min cell temperature (°C)
    temp_high: float | None = None  # Thigh – max cell temperature (°C)
    volt_low: float | None = None  # Vlow  – min cell voltage (V)
    volt_high: float | None = None  # Vhigh – max cell voltage (V)
    volt_status: str | None = None  # Volt.St
    curr_status: str | None = None  # Curr.St
    temp_status: str | None = None  # Temp.St
    batt_volt_status: str | None = None  # B.V.St – battery-level voltage state
    batt_temp_status: str | None = None  # B.T.St – battery-level temperature state

    cells: list[PylontechCell] = field(default_factory=list)


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
    cell_count: int | None = None
    spec: str | None = None
    barcode: str | None = None
    fw_version: str | None = None
    soft_version: str | None = None
    board_version: str | None = None
    boot_version: str | None = None
    comm_version: str | None = None
    release_date: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    max_charge_curr: float | None = None  # A
    max_dischg_curr: float | None = None  # A

    # Time Command Data
    bms_time: str | None = None

    # Stat Command Data
    cycles: int | None = None
    soh: int | None = None
    charge_times: int | None = None
    discharge_cnt: int | None = None
    idle_times: int | None = None
    shut_times: int | None = None
    reset_times: int | None = None
    sc_times: int | None = None  # short circuit events
    bat_ov_times: int | None = None  # battery overvoltage
    bat_hv_times: int | None = None  # battery high voltage
    bat_lv_times: int | None = None  # battery low voltage
    bat_uv_times: int | None = None  # battery undervoltage
    pwr_ov_times: int | None = None  # power overvoltage
    pwr_hv_times: int | None = None  # power high voltage
    life_warn_times: int | None = None
    life_alarm_times: int | None = None
    pwr_coulomb: int | None = None  # total mAh throughput
    dsg_cap: int | None = None  # discharge capacity (mAh)

    raw: str = ""

    batteries: list[PylontechBattery] = field(default_factory=list)

    @property
    def battery_count(self) -> int:
        return len(self.batteries)
