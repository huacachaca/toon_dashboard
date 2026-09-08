from __future__ import annotations

import math
from dataclasses import dataclass
from calendar import isleap
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from .models import Reading


@dataclass(frozen=True)
class SolarAdvice:
    panel_count: int
    installed_kwp: float
    expected_yield_kwh: float
    coverage_percent: float


def readings_frame(readings: list[Reading]) -> pd.DataFrame:
    if not readings:
        return pd.DataFrame(columns=["ts", "stream", "value", "source_file"])
    frame = pd.DataFrame([reading.__dict__ for reading in readings])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    return frame.set_index("ts").sort_index()


def series(
    readings: list[Reading],
    stream: str,
    granularity: str,
    start: datetime | None = None,
    end: datetime | None = None,
    timezone_name: str = "Europe/Amsterdam",
) -> pd.DataFrame:
    frame = readings_frame(readings)
    if stream == "electricity":
        frame = frame[frame["stream"].isin(["elec_day", "elec_night"])]
    else:
        frame = frame[frame["stream"] == "gas"]
    if not frame.empty:
        if start is not None:
            frame = frame[frame.index >= _utc_timestamp(start)]
        if end is not None:
            frame = frame[frame.index < _utc_timestamp(end)]
    rule = {"hour": "h", "day": "D", "week": "W-MON", "month": "MS", "year": "YS"}[granularity]
    if start is not None and end is not None:
        local_start = pd.Timestamp(start).tz_convert(timezone_name) if pd.Timestamp(start).tzinfo else pd.Timestamp(start, tz=timezone_name)
        local_end = pd.Timestamp(end).tz_convert(timezone_name) if pd.Timestamp(end).tzinfo else pd.Timestamp(end, tz=timezone_name)
        expected_index = pd.date_range(local_start, local_end, inclusive="left", freq=rule)
    else:
        expected_index = None
    if frame.empty:
        grouped = pd.DataFrame(index=expected_index)
    else:
        frame.index = frame.index.tz_convert(timezone_name)
        grouped = frame.pivot_table(index=frame.index, columns="stream", values="value", aggfunc="sum")
        grouped = grouped.resample(rule).sum(min_count=1).fillna(0)
        if expected_index is not None:
            grouped = grouped.reindex(expected_index, fill_value=0.0)
    if stream == "electricity":
        for column in ("elec_day", "elec_night"):
            if column not in grouped:
                grouped[column] = 0.0
        if granularity == "hour":
            mixed_tariff = (grouped["elec_day"] > 0) & (grouped["elec_night"] > 0)
            day_dominant = mixed_tariff & (grouped["elec_day"] >= grouped["elec_night"])
            night_dominant = mixed_tariff & ~day_dominant
            grouped.loc[day_dominant, "elec_day"] += grouped.loc[day_dominant, "elec_night"]
            grouped.loc[day_dominant, "elec_night"] = 0.0
            grouped.loc[night_dominant, "elec_night"] += grouped.loc[night_dominant, "elec_day"]
            grouped.loc[night_dominant, "elec_day"] = 0.0
        grouped["total"] = grouped["elec_day"] + grouped["elec_night"]
    else:
        if "gas" not in grouped:
            grouped["gas"] = 0.0
    return grouped


def _utc_timestamp(value: datetime) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def zoom_window(selected_date: date, zoom: str, timezone_name: str = "Europe/Amsterdam") -> tuple[datetime, datetime, str]:
    """Return the local-time window and chart resolution for a selected date."""
    timezone_info = ZoneInfo(timezone_name)
    day_start = datetime.combine(selected_date, time.min, tzinfo=timezone_info)
    if zoom == "day":
        return day_start, day_start + timedelta(days=1), "hour"
    if zoom == "week":
        start = day_start - timedelta(days=day_start.weekday())
        return start, start + timedelta(days=7), "day"
    if zoom == "month":
        next_month = (day_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return day_start.replace(day=1), next_month, "day"
    if zoom == "year":
        return day_start.replace(month=1, day=1), day_start.replace(year=day_start.year + 1, month=1, day=1), "month"
    raise ValueError(f"unsupported zoom: {zoom}")


def analysis_rows(
    readings: list[Reading],
    year: int,
    granularity: str = "month",
    timezone_name: str = "Europe/Amsterdam",
) -> list[dict[str, object]]:
    timezone_info = ZoneInfo(timezone_name)
    start = datetime(year - 1, 12, 1, tzinfo=timezone_info)
    end = datetime(year + 1, 1, 1, tzinfo=timezone_info)
    grouped = series(readings, "electricity", granularity, start, end, timezone_name)
    if grouped.empty:
        return []
    rows: list[dict[str, object]] = []
    previous_total: float | None = None
    for timestamp, row in grouped.iterrows():
        if timestamp.year != year:
            previous_total = float(row["total"])
            continue
        total = float(row["total"])
        day = float(row["elec_day"])
        night = float(row["elec_night"])
        rows.append({
            "period": timestamp.strftime("%d-%m-%Y"),
            "day": day,
            "night": night,
            "total": total,
            "night_share": (night / total * 100) if total else 0.0,
            "avg_kw": total / (24 * 7 if granularity == "week" else 24 * 30 if granularity == "month" else 24),
            "change": ((total - previous_total) / previous_total * 100) if previous_total else None,
        })
        previous_total = total
    return rows


def next_quarter_start(value: date | datetime, timezone_name: str = "Europe/Amsterdam") -> pd.Timestamp:
    local_value = pd.Timestamp(value)
    if local_value.tzinfo is None:
        local_value = local_value.tz_localize(timezone_name)
    else:
        local_value = local_value.tz_convert(timezone_name)
    next_month = ((local_value.month - 1) // 3) * 3 + 4
    next_year = local_value.year + (next_month - 1) // 12
    next_month = (next_month - 1) % 12 + 1
    return pd.Timestamp(datetime(next_year, next_month, 1, tzinfo=ZoneInfo(timezone_name)))


def forecast_series(
    readings: list[Reading],
    timezone_name: str = "Europe/Amsterdam",
    quarters: int = 8,
    forecast_start: date | datetime | None = None,
) -> pd.DataFrame:
    """Forecast total electricity kWh and gas m3 from local daily usage."""
    frame = readings_frame(readings)
    if frame.empty:
        return pd.DataFrame(columns=["electricity", "gas"])
    frame.index = frame.index.tz_convert(timezone_name)
    grouped = frame.pivot_table(index=frame.index, columns="stream", values="value", aggfunc="sum")
    daily = grouped.resample("D").sum(min_count=1)
    for column in ("elec_day", "elec_night", "gas"):
        if column not in daily:
            daily[column] = 0.0
    daily_usage = pd.DataFrame({
        "electricity": daily["elec_day"] + daily["elec_night"],
        "gas": daily["gas"],
    }).dropna(how="all")
    if daily_usage.empty:
        return pd.DataFrame(columns=["electricity", "gas"])

    if forecast_start is None:
        first_future_quarter = next_quarter_start(datetime.now(ZoneInfo(timezone_name)), timezone_name)
    else:
        first_future_quarter = pd.Timestamp(forecast_start)
        if first_future_quarter.tzinfo is None:
            first_future_quarter = first_future_quarter.tz_localize(timezone_name)
        else:
            first_future_quarter = first_future_quarter.tz_convert(timezone_name)
        first_future_quarter = first_future_quarter.normalize()
    daily_usage = daily_usage[daily_usage.index < first_future_quarter]
    if daily_usage.empty:
        return pd.DataFrame(columns=["electricity", "gas"])

    daily_usage["contract_year"] = daily_usage.index.year - (daily_usage.index.month < 8).astype(int)
    annual = daily_usage.groupby("contract_year")[['electricity', 'gas']].sum(min_count=1)
    latest_contract_year = int(daily_usage["contract_year"].iloc[-1])
    complete_annual = annual.drop(index=latest_contract_year, errors="ignore")
    growth = complete_annual.pct_change().replace([float("inf"), -float("inf")], float("nan")).median().fillna(0.0)
    growth = growth.clip(lower=-0.20, upper=0.20)
    daily_usage = _remove_daily_outliers(daily_usage)
    daily_usage["quarter"] = daily_usage.index.quarter
    seasonal_daily = daily_usage.groupby("quarter")[['electricity', 'gas']].mean()
    seasonal_daily = seasonal_daily.reindex(range(1, 5)).fillna(daily_usage[['electricity', 'gas']].mean())
    future_index = pd.DatetimeIndex(
        [first_future_quarter + pd.DateOffset(months=3 * offset) for offset in range(quarters)]
    )
    forecast = pd.DataFrame(index=future_index, columns=["electricity", "gas"], dtype=float)
    for timestamp in forecast.index:
        quarter_days = (timestamp + pd.offsets.QuarterBegin(startingMonth=1) - timestamp).days
        quarter = timestamp.quarter
        contract_year = timestamp.year - int(timestamp.month < 8)
        years_ahead = contract_year - latest_contract_year
        trend = (1 + growth) ** years_ahead
        forecast.loc[timestamp] = seasonal_daily.loc[quarter] * quarter_days * trend
    forecast.attrs["annual_growth"] = growth.to_dict()
    forecast.attrs["historical_end"] = daily_usage.index[-1]
    forecast.attrs["forecast_start"] = first_future_quarter
    forecast.attrs["historical_contract_year_totals"] = annual.tail(5).to_dict("index")
    return forecast


def usage_between(
    readings: list[Reading],
    start: date,
    end: pd.Timestamp,
    timezone_name: str = "Europe/Amsterdam",
) -> dict[str, float]:
    """Return stored usage after a meter baseline date and before forecast start."""
    frame = readings_frame(readings)
    if frame.empty:
        return {"electricity": 0.0, "gas": 0.0}
    frame.index = frame.index.tz_convert(timezone_name)
    start_timestamp = pd.Timestamp(start, tz=timezone_name)
    selected = frame[(frame.index >= start_timestamp) & (frame.index < end)]
    return {
        "electricity": float(selected.loc[selected["stream"].isin(["elec_day", "elec_night"]), "value"].sum()),
        "gas": float(selected.loc[selected["stream"] == "gas", "value"].sum()),
    }


def forecast_contract_year_rows(forecast: pd.DataFrame, start_month: int = 8) -> list[dict[str, object]]:
    """Summarize forecast totals for contract years beginning on August 1."""
    if forecast.empty:
        return []
    frame = forecast.copy()
    frame["contract_year"] = frame.index.year - (frame.index.month < start_month).astype(int)
    rows: list[dict[str, object]] = []
    for contract_year, group in frame.groupby("contract_year", sort=True):
        rows.append({
            "period": f"01-08-{contract_year} t/m 31-07-{contract_year + 1}",
            "electricity": float(group["electricity"].sum()),
            "gas": float(group["gas"].sum()),
        })
    return rows


def apply_forecast_electricity_adjustments(
    forecast: pd.DataFrame,
    *,
    induction: bool = False,
    heat_pump: bool = False,
    induction_kwh: float = 200.0,
    heat_pump_kwh: float = 3000.0,
) -> pd.DataFrame:
    """Add optional annual electricity loads, distributed by quarter length."""
    adjusted = forecast.copy()
    annual_increment = (induction_kwh if induction else 0.0) + (heat_pump_kwh if heat_pump else 0.0)
    if adjusted.empty or not annual_increment:
        return adjusted
    contract_year = adjusted.index.year - (adjusted.index.month < 8).astype(int)
    for year, positions in pd.Series(range(len(adjusted)), index=adjusted.index).groupby(contract_year):
        year_index = positions.index
        quarter_days = pd.Series(
            [(timestamp + pd.offsets.QuarterBegin(startingMonth=1) - timestamp).days for timestamp in year_index],
            index=year_index,
            dtype=float,
        )
        total_days = float(quarter_days.sum())
        adjusted.loc[year_index, "electricity"] += annual_increment * quarter_days / total_days
    return adjusted


def annualized_usage(
    readings: list[Reading],
    stream: str,
    timezone_name: str = "Europe/Amsterdam",
) -> float:
    """Return the latest trailing-year usage, based on local calendar days."""
    frame = readings_frame(readings)
    if frame.empty:
        return 0.0
    frame.index = frame.index.tz_convert(timezone_name)
    if stream == "electricity":
        frame = frame[frame["stream"].isin(["elec_day", "elec_night"])]
        daily = frame.groupby(frame.index.date)["value"].sum()
    else:
        frame = frame[frame["stream"] == stream]
        daily = frame.groupby(frame.index.date)["value"].sum()
    if daily.empty:
        return 0.0
    daily.index = pd.DatetimeIndex(daily.index).tz_localize(timezone_name)
    end = daily.index.max().normalize() + timedelta(days=1)
    start = end - timedelta(days=365)
    observed = daily[(daily.index >= start) & (daily.index < end)]
    if observed.empty:
        return 0.0
    total = float(observed.sum())
    span_days = max((observed.index.max().date() - observed.index.min().date()).days + 1, 1)
    return total * 365 / span_days if span_days < 365 else total


def _remove_daily_outliers(frame: pd.DataFrame) -> pd.DataFrame:
    """Exclude counter-gap spikes from seasonal daily-rate estimates."""
    valid = pd.Series(True, index=frame.index)
    for column in frame.columns:
        values = frame[column].dropna()
        if values.empty:
            continue
        lower = values.quantile(0.25)
        upper = values.quantile(0.75)
        threshold = upper + 6 * (upper - lower)
        valid &= frame[column].fillna(0) <= threshold
    return frame[valid]


def gas_analysis_rows(readings: list[Reading], year: int, timezone_name: str = "Europe/Amsterdam") -> list[dict[str, object]]:
    start = datetime(year, 1, 1, tzinfo=ZoneInfo(timezone_name))
    end = datetime(year + 1, 1, 1, tzinfo=ZoneInfo(timezone_name))
    grouped = series(readings, "gas", "month", start, end, timezone_name)
    if grouped.empty:
        return []
    rows: list[dict[str, object]] = []
    previous: float | None = None
    for timestamp, row in grouped.iterrows():
        total = float(row["gas"])
        rows.append({
            "period": timestamp.strftime("%d-%m-%Y"),
            "total": total,
            "average": total / 30,
            "change": ((total - previous) / previous * 100) if previous else None,
        })
        previous = total
    return rows


def solar_advice(annual_kwh: float, panel_wp: float = 455.0, yearly_yield_kwh: float | None = None, specific_yield_kwh_per_kwp: float = 880.0, roof_area_m2: float | None = None, panel_area_m2: float = 1.998, target_percent: float = 75.0) -> dict[str, SolarAdvice]:
    yield_per_panel = yearly_yield_kwh if yearly_yield_kwh is not None else panel_wp / 1000 * specific_yield_kwh_per_kwp
    target_kwh = annual_kwh * target_percent / 100
    target_count = math.ceil(target_kwh / yield_per_panel) if target_kwh > 0 else 0
    scenarios = {
        "target": target_count,
    }
    if roof_area_m2 is not None:
        scenarios["roof"] = max(0, math.floor(roof_area_m2 / panel_area_m2))
    advice: dict[str, SolarAdvice] = {}
    for name, count in scenarios.items():
        expected = count * yield_per_panel
        advice[name] = SolarAdvice(count, count * panel_wp / 1000, expected, expected / annual_kwh * 100 if annual_kwh else 0.0)
    return advice
