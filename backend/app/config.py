from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    toon_host: str = Field(default="<<ipaddress>>", validation_alias="TOON_HOST")
    data_dir: Path = Path("data")
    electricity_counter_divisor: float = 1000.0
    gas_counter_divisor: float = 1000.0
    solar_panel_name: str = "JA Solar JAM54D41-455/LB"
    panel_wp: float = 455.0
    solar_panel_yearly_yield_kwh: float | None = None
    specific_yield_kwh_per_kwp: float = 880.0
    panel_area_m2: float = 1.998
    solar_target_percent: float = 75.0
    contract_start_month: int = Field(default=8, ge=1, le=12)
    forecast_electricity_tn_base: float = 0.0
    forecast_electricity_tl_base: float = 0.0
    forecast_gas_base: float = 0.0
    forecast_base_date: date = date(2026, 8, 1)
    forecast_induction_kwh: float = 200.0
    forecast_heat_pump_kwh: float = 3000.0
    timezone: str = "Europe/Amsterdam"
    refresh_timeout_seconds: float = 30.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TOON_",
        env_ignore_empty=True,
        extra="ignore",
    )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "toon.sqlite3"

    @property
    def contract_end_month(self) -> int:
        return self.contract_start_month - 1 or 12


@lru_cache
def get_settings() -> Settings:
    return Settings()
