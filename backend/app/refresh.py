from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings
from .parser import parse_export
from .store import Store
from .toon_client import download_export


@dataclass(frozen=True)
class RefreshResult:
    imported_rows: int
    archive_path: str
    refreshed_at: datetime


def refresh_data(settings: Settings, store: Store) -> RefreshResult:
    archive, archive_path = download_export(settings.toon_host, settings.raw_dir, settings.refresh_timeout_seconds)
    readings = parse_export(
        archive,
        timezone_name=settings.timezone,
        electricity_counter_divisor=settings.electricity_counter_divisor,
        gas_counter_divisor=settings.gas_counter_divisor,
    )
    imported_rows = store.upsert_readings(readings)
    refreshed_at = datetime.now(timezone.utc)
    store.record_export(refreshed_at, str(archive_path), imported_rows)
    return RefreshResult(imported_rows, str(archive_path), refreshed_at)
