from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, Float, Integer, MetaData, String, Table, create_engine, func, select
from sqlalchemy.dialects.sqlite import insert

from .models import Reading


class Store:
    def __init__(self, database_path: Path, timezone_name: str = "Europe/Amsterdam") -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{database_path}", future=True)
        self.timezone = ZoneInfo(timezone_name)
        self.metadata = MetaData()
        self.readings = Table(
            "readings",
            self.metadata,
            # SQLite stores timezone-aware values as ISO strings; values are normalized on read.
            __import__("sqlalchemy").Column("ts", DateTime(timezone=True), primary_key=True),
            __import__("sqlalchemy").Column("stream", String(20), primary_key=True),
            __import__("sqlalchemy").Column("value", Float, nullable=False),
            __import__("sqlalchemy").Column("source_file", String(255), nullable=False),
        )
        self.export_history = Table(
            "export_history",
            self.metadata,
            __import__("sqlalchemy").Column("id", Integer, primary_key=True, autoincrement=True),
            __import__("sqlalchemy").Column("loaded_at", DateTime(timezone=True), nullable=False),
            __import__("sqlalchemy").Column("archive_path", String(512), nullable=False),
            __import__("sqlalchemy").Column("imported_rows", Integer, nullable=False),
        )
        self.metadata_store = Table(
            "metadata_store",
            self.metadata,
            __import__("sqlalchemy").Column("key", String(100), primary_key=True),
            __import__("sqlalchemy").Column("value", String(255), nullable=False),
        )
        self.metadata.create_all(self.engine)
        self._normalize_timestamps()
        self._normalize_electricity_tariffs()

    def _normalize_timestamps(self) -> None:
        with self.engine.begin() as connection:
            migrated = connection.execute(
                select(self.metadata_store.c.value).where(self.metadata_store.c.key == "timestamps_utc_v1")
            ).scalar_one_or_none()
            if migrated is not None:
                return
            rows = connection.execute(select(self.readings.c.ts, self.readings.c.stream)).all()
            for timestamp, stream in rows:
                if timestamp.tzinfo is None:
                    utc_timestamp = timestamp.replace(tzinfo=self.timezone).astimezone(timezone.utc).replace(tzinfo=None)
                    connection.execute(
                        self.readings.update()
                        .where(self.readings.c.ts == timestamp)
                        .where(self.readings.c.stream == stream)
                        .values(ts=utc_timestamp)
                    )
            connection.execute(self.metadata_store.insert().values(key="timestamps_utc_v1", value="1"))

    def _normalize_electricity_tariffs(self) -> None:
        """Repair rows imported before lt/nt were mapped to their tariff meanings."""
        with self.engine.begin() as connection:
            connection.execute(
                self.readings.update()
                .where(self.readings.c.stream == "elec_day")
                .values(stream="elec_day_legacy")
            )
            connection.execute(
                self.readings.update()
                .where(self.readings.c.stream == "elec_night")
                .values(stream="elec_night_legacy")
            )
            connection.execute(
                self.readings.update()
                .where(self.readings.c.source_file.like("%elec_quantity_lt_%"))
                .values(stream="elec_night")
            )
            connection.execute(
                self.readings.update()
                .where(self.readings.c.source_file.like("%elec_quantity_nt_%"))
                .values(stream="elec_day")
            )

    def all_readings(self) -> list[Reading]:
        statement = select(self.readings).order_by(self.readings.c.ts, self.readings.c.stream)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [
            Reading(row["ts"].replace(tzinfo=timezone.utc), row["stream"], row["value"], row["source_file"])
            for row in rows
        ]

    def coverage(self) -> tuple[datetime | None, datetime | None, int]:
        statement = select(func.min(self.readings.c.ts), func.max(self.readings.c.ts), func.count())
        with self.engine.connect() as connection:
            first, last, count = connection.execute(statement).one()
        return first, last, count

    @staticmethod
    def _storage_timestamp(timestamp: datetime) -> datetime:
        return timestamp.astimezone(timezone.utc).replace(tzinfo=None) if timestamp.tzinfo else timestamp

    def record_export(self, loaded_at: datetime, archive_path: str, imported_rows: int) -> None:
        with self.engine.begin() as connection:
            connection.execute(self.export_history.insert().values(
                loaded_at=loaded_at,
                archive_path=archive_path,
                imported_rows=imported_rows,
            ))

    def recent_exports(self, limit: int = 30) -> list[dict[str, object]]:
        statement = select(self.export_history).order_by(self.export_history.c.loaded_at.desc()).limit(limit)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(statement).mappings().all()]

    def upsert_readings(self, readings: list[Reading]) -> int:
        if not readings:
            return 0
        values = [
            {"ts": self._storage_timestamp(reading.ts), "stream": reading.stream, "value": reading.value, "source_file": reading.source_file}
            for reading in readings
        ]
        with self.engine.begin() as connection:
            for start in range(0, len(values), 250):
                batch = values[start:start + 250]
                statement = insert(self.readings).values(batch)
                statement = statement.on_conflict_do_update(
                    index_elements=[self.readings.c.ts, self.readings.c.stream],
                    set_={"value": statement.excluded.value, "source_file": statement.excluded.source_file},
                )
                connection.execute(statement)
        return len(values)
