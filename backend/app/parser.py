from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Iterable
from zoneinfo import ZoneInfo

from .models import Reading, Stream


class ExportParseError(ValueError):
    """Raised when a Toon export does not match a supported layout."""


_STREAM_PATTERNS: tuple[tuple[Stream, re.Pattern[str]], ...] = (
    ("elec_night", re.compile(r"^elec_quantity_lt_.*_5yrhours\.csv$")),
    ("elec_day", re.compile(r"^elec_quantity_nt_.*_5yrhours\.csv$")),
    ("gas", re.compile(r"^gas_quantity_.*_5yrhours\.csv$")),
)


def parse_export(
    archive: bytes,
    *,
    timezone_name: str = "Europe/Amsterdam",
    electricity_counter_divisor: float = 1000.0,
    gas_counter_divisor: float = 1000.0,
) -> list[Reading]:
    """Parse the nested Toon export into interval readings.

    Toon usage CSVs contain Unix timestamps and cumulative counters. The first
    counter is a baseline and is not emitted as an interval reading.
    """
    if electricity_counter_divisor <= 0 or gas_counter_divisor <= 0:
        raise ValueError("counter divisors must be positive")

    try:
        timezone_info = ZoneInfo(timezone_name)
    except Exception as error:
        raise ValueError(f"unknown timezone: {timezone_name}") from error

    with _open_zip(archive, "export.zip") as outer:
        usage_name = _find_member(outer.namelist(), "usage.zip")
        if usage_name is None:
            raise ExportParseError("export does not contain usage.zip")
        usage_bytes = outer.read(usage_name)

    readings: list[Reading] = []
    with _open_zip(usage_bytes, "usage.zip") as usage:
        for stream, pattern in _STREAM_PATTERNS:
            members = [name for name in usage.namelist() if pattern.match(PurePosixPath(name).name)]
            if not members:
                continue
            primary_members = [name for name in members if "_orig_" not in PurePosixPath(name).name]
            if primary_members:
                members = primary_members
            if len(members) > 1:
                raise ExportParseError(f"multiple hourly files found for {stream}: {members}")
            divisor = gas_counter_divisor if stream == "gas" else electricity_counter_divisor
            readings.extend(_parse_counter_csv(usage.read(members[0]), members[0], stream, timezone_info, divisor))

    if not readings:
        raise ExportParseError("usage.zip contains no supported hourly electricity or gas streams")
    return sorted(readings, key=lambda reading: (reading.ts, reading.stream))


def _open_zip(data: bytes, label: str) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        if archive.testzip() is not None:
            archive.close()
            raise ExportParseError(f"{label} contains corrupt data")
        return archive
    except zipfile.BadZipFile as error:
        raise ExportParseError(f"{label} is not a valid ZIP archive") from error


def _find_member(names: Iterable[str], expected_name: str) -> str | None:
    for name in names:
        if PurePosixPath(name).name == expected_name:
            return name
    return None


def _parse_counter_csv(
    data: bytes,
    source_file: str,
    stream: Stream,
    timezone_info: ZoneInfo,
    divisor: float,
) -> list[Reading]:
    previous_timestamp: int | None = None
    previous_counter: float | None = None
    readings: list[Reading] = []
    text = data.decode("utf-8-sig")

    for row_number, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not row or all(not field.strip() for field in row):
            continue
        if len(row) != 2:
            raise ExportParseError(f"{source_file}:{row_number} must contain timestamp,counter")
        timestamp_text, counter_text = (field.strip() for field in row)
        if timestamp_text == "-" or counter_text == "-":
            continue
        try:
            timestamp = int(timestamp_text)
            counter = float(counter_text)
        except ValueError as error:
            raise ExportParseError(f"{source_file}:{row_number} contains non-numeric data") from error
        if timestamp <= 0:
            raise ExportParseError(f"{source_file}:{row_number} has an invalid timestamp")
        if previous_timestamp is not None:
            if timestamp <= previous_timestamp:
                raise ExportParseError(f"{source_file}:{row_number} timestamps are not increasing")
            if previous_counter is not None and counter < previous_counter:
                previous_timestamp = timestamp
                previous_counter = counter
                continue
            if previous_counter is not None:
                interval_value = (counter - previous_counter) / divisor
                interval_start = datetime.fromtimestamp(previous_timestamp, timezone.utc).astimezone(timezone_info)
                readings.append(Reading(interval_start, stream, interval_value, source_file))
        previous_timestamp = timestamp
        previous_counter = counter
    return readings
