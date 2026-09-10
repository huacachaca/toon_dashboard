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
    annual_net_surplus_kwh: float = 0.0
    estimated_feed_in_eur_per_year: float = 0.0


# Compass azimuth in degrees, 0 = north, clockwise (matches solar-position convention below).
COMPASS_AZIMUTH: dict[str, float] = {
    "N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0,
    "S": 180.0, "SW": 225.0, "W": 270.0, "NW": 315.0,
}


def _solar_position(timestamp_utc: pd.Timestamp, latitude_deg: float, longitude_deg: float) -> tuple[float, float]:
    """Return (elevation, azimuth) in degrees using the NOAA solar-position approximation."""
    day_of_year = timestamp_utc.dayofyear
    hour_utc = timestamp_utc.hour + timestamp_utc.minute / 60 + timestamp_utc.second / 3600
    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hour_utc - 12) / 24)
    equation_of_time = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )
    true_solar_time = hour_utc * 60 + equation_of_time + 4 * longitude_deg
    hour_angle = math.radians(true_solar_time / 4 - 180)
    lat = math.radians(latitude_deg)
    cos_zenith = max(-1.0, min(1.0, math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(declination) * math.cos(hour_angle)))
    zenith = math.acos(cos_zenith)
    elevation = 90 - math.degrees(zenith)
    if math.sin(zenith) == 0:
        return elevation, 180.0
    cos_azimuth = max(-1.0, min(1.0, (math.sin(declination) - math.sin(lat) * cos_zenith) / (math.cos(lat) * math.sin(zenith))))
    azimuth = math.degrees(math.acos(cos_azimuth))
    azimuth = 360 - azimuth if hour_angle > 0 else azimuth
    return elevation, azimuth


def _clear_sky_irradiance(elevation_deg: float, day_of_year: int) -> tuple[float, float]:
    """Rough clear-sky direct-normal and diffuse-horizontal irradiance (W/m2), no cloud cover."""
    if elevation_deg <= 0:
        return 0.0, 0.0
    zenith = math.radians(90 - elevation_deg)
    air_mass = 1 / max(math.cos(zenith), 0.02)
    extraterrestrial = 1361 * (1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365))
    direct_normal = extraterrestrial * 0.7 ** (air_mass ** 0.678)
    diffuse_horizontal = 0.1 * direct_normal * math.cos(zenith)
    return direct_normal, diffuse_horizontal


def _plane_of_array_irradiance(elevation_deg: float, sun_azimuth_deg: float, tilt_deg: float, panel_azimuth_deg: float, direct_normal: float, diffuse_horizontal: float) -> float:
    """Transpose horizontal clear-sky irradiance onto a tilted, oriented panel plane (isotropic-sky model)."""
    if elevation_deg <= 0:
        return 0.0
    zenith = math.radians(90 - elevation_deg)
    tilt = math.radians(tilt_deg)
    azimuth_diff = math.radians(sun_azimuth_deg - panel_azimuth_deg)
    cos_incidence = math.cos(zenith) * math.cos(tilt) + math.sin(zenith) * math.sin(tilt) * math.cos(azimuth_diff)
    direct_poa = direct_normal * max(cos_incidence, 0.0)
    diffuse_poa = diffuse_horizontal * (1 + math.cos(tilt)) / 2
    return direct_poa + diffuse_poa


def solar_production_series(
    start: datetime,
    end: datetime,
    granularity: str,
    panel_count: int,
    orientation: str,
    latitude_deg: float,
    longitude_deg: float,
    tilt_deg: float,
    panel_wp: float,
    panel_area_m2: float,
    performance_ratio: float,
    clearness_factor: float,
    timezone_name: str = "Europe/Amsterdam",
    secondary_orientation: str | None = None,
    secondary_share_percent: float = 0.0,
) -> pd.Series:
    """Modeled clear-sky solar production per bucket (kWh); an estimate, not a measurement.

    A second roof face can be given independently of the primary orientation (e.g. a ridge
    that isn't exactly east-west, or an uneven panel split), rather than assuming a fixed
    50/50 east-west split.
    """
    timezone_info = ZoneInfo(timezone_name)
    rule = {"hour": "h", "day": "D", "week": "W-MON", "month": "MS", "year": "YS"}[granularity]
    local_start = pd.Timestamp(start).tz_convert(timezone_info) if pd.Timestamp(start).tzinfo else pd.Timestamp(start, tz=timezone_info)
    local_end = pd.Timestamp(end).tz_convert(timezone_info) if pd.Timestamp(end).tzinfo else pd.Timestamp(end, tz=timezone_info)
    target_index = pd.date_range(local_start, local_end, inclusive="left", freq=rule)
    if panel_count <= 0:
        return pd.Series(0.0, index=target_index, name="production")
    hourly_index = pd.date_range(local_start, local_end, inclusive="left", freq="h")
    secondary_share = max(0.0, min(secondary_share_percent, 100.0)) / 100 if secondary_orientation else 0.0
    azimuth_weights = [(COMPASS_AZIMUTH[orientation], 1 - secondary_share)]
    if secondary_share > 0:
        azimuth_weights.append((COMPASS_AZIMUTH[secondary_orientation], secondary_share))
    efficiency = panel_wp / 1000 / panel_area_m2
    values = []
    for timestamp in hourly_index:
        timestamp_utc = timestamp.tz_convert("UTC")
        elevation, azimuth = _solar_position(timestamp_utc, latitude_deg, longitude_deg)
        if elevation <= 0:
            values.append(0.0)
            continue
        direct_normal, diffuse_horizontal = _clear_sky_irradiance(elevation, timestamp_utc.dayofyear)
        poa = sum(
            weight * _plane_of_array_irradiance(elevation, azimuth, tilt_deg, panel_azimuth, direct_normal, diffuse_horizontal)
            for panel_azimuth, weight in azimuth_weights
        )
        power_w = poa * panel_count * panel_area_m2 * efficiency * performance_ratio * clearness_factor
        values.append(max(power_w, 0.0) / 1000)
    hourly = pd.Series(values, index=hourly_index, name="production")
    if granularity == "hour":
        return hourly.reindex(target_index, fill_value=0.0)
    return hourly.resample(rule).sum().reindex(target_index, fill_value=0.0)


def _panel_production_kwargs(settings: object) -> dict[str, object]:
    return dict(
        latitude_deg=settings.solar_latitude,
        longitude_deg=settings.solar_longitude,
        tilt_deg=settings.solar_panel_tilt_degrees,
        panel_wp=settings.panel_wp,
        panel_area_m2=settings.panel_area_m2,
        performance_ratio=settings.solar_system_performance_ratio,
        clearness_factor=settings.solar_clearness_factor,
        timezone_name=settings.timezone,
    )


def offset_series(
    readings: list[Reading],
    granularity: str,
    start: datetime,
    end: datetime,
    panel_count: int,
    orientation: str,
    settings: object,
    secondary_orientation: str | None = None,
    secondary_share_percent: float = 0.0,
) -> pd.DataFrame:
    """Electricity usage combined with modeled solar production: grid import/export offset."""
    usage = series(readings, "electricity", granularity, start, end, settings.timezone)
    production = solar_production_series(start, end, granularity, panel_count, orientation, **_panel_production_kwargs(settings), secondary_orientation=secondary_orientation, secondary_share_percent=secondary_share_percent)
    frame = usage.reindex(production.index, fill_value=0.0) if not usage.empty else pd.DataFrame(index=production.index, columns=["elec_day", "elec_night", "total"]).fillna(0.0)
    frame["production"] = production
    frame["from_grid"] = (frame["total"] - frame["production"]).clip(lower=0.0)
    frame["returned_to_grid"] = (frame["production"] - frame["total"]).clip(lower=0.0)
    return frame


def minimal_panels_for_sun_coverage(
    readings: list[Reading],
    orientation: str,
    settings: object,
    target_percent: float = 75.0,
    max_panels: int = 40,
    secondary_orientation: str | None = None,
    secondary_share_percent: float = 0.0,
) -> dict[str, object]:
    """Smallest panel count whose modeled production covers target_percent of usage during sunlit hours.

    More panels beyond that point only add export, not daytime coverage, so the smallest
    count reaching the target also minimizes returned-to-grid kWh among counts that qualify.
    """
    frame = readings_frame(readings)
    frame = frame[frame["stream"].isin(["elec_day", "elec_night"])]
    empty_result = {"panel_count": 0, "coverage_percent": 0.0, "returned_to_grid_kwh": 0.0, "self_consumption_of_production_percent": 0.0, "annual_net_surplus_kwh": 0.0, "estimated_feed_in_eur_per_year": 0.0, "reachable": False}
    if frame.empty:
        return empty_result
    frame.index = frame.index.tz_convert(settings.timezone)
    hourly_usage = frame["value"].resample("h").sum(min_count=1).fillna(0.0)
    end = hourly_usage.index.max().normalize() + timedelta(days=1)
    start = end - timedelta(days=365)
    hourly_usage = hourly_usage[(hourly_usage.index >= start) & (hourly_usage.index < end)]
    if hourly_usage.empty:
        return empty_result
    per_panel = solar_production_series(
        start.to_pydatetime(), end.to_pydatetime(), "hour", 1, orientation, **_panel_production_kwargs(settings),
        secondary_orientation=secondary_orientation, secondary_share_percent=secondary_share_percent,
    ).reindex(hourly_usage.index, fill_value=0.0)
    sun_hours = per_panel > 0
    usage_during_sun = hourly_usage[sun_hours]
    if usage_during_sun.sum() <= 0:
        return empty_result
    for count in range(0, max_panels + 1):
        production = per_panel * count
        self_consumed = pd.concat([production[sun_hours], usage_during_sun], axis=1).min(axis=1)
        coverage = float(self_consumed.sum() / usage_during_sun.sum() * 100)
        if coverage >= target_percent:
            return _sun_coverage_result(count, coverage, production, hourly_usage, settings, reachable=True)
    production = per_panel * max_panels
    self_consumed = pd.concat([production[sun_hours], usage_during_sun], axis=1).min(axis=1)
    coverage = float(self_consumed.sum() / usage_during_sun.sum() * 100)
    return _sun_coverage_result(max_panels, coverage, production, hourly_usage, settings, reachable=False)


def _sun_coverage_result(panel_count: int, coverage_percent: float, production: pd.Series, hourly_usage: pd.Series, settings: object, reachable: bool) -> dict[str, object]:
    """Bundle usage-side coverage, production-side self-consumption, and the annual net surplus a feed-in tariff applies to under salderen."""
    returned = (production - hourly_usage).clip(lower=0.0)
    self_consumed_all = pd.concat([production, hourly_usage], axis=1).min(axis=1)
    production_total = float(production.sum())
    annual_net_surplus = max(production_total - float(hourly_usage.sum()), 0.0)
    return {
        "panel_count": panel_count,
        "coverage_percent": coverage_percent,
        "returned_to_grid_kwh": float(returned.sum()),
        "self_consumption_of_production_percent": float(self_consumed_all.sum() / production_total * 100) if production_total else 0.0,
        "annual_net_surplus_kwh": annual_net_surplus,
        "estimated_feed_in_eur_per_year": annual_net_surplus * settings.feed_in_tariff_eur_per_kwh,
        "reachable": reachable,
    }


def offset_analysis_rows(frame: pd.DataFrame) -> list[dict[str, object]]:
    if frame.empty:
        return []
    rows: list[dict[str, object]] = []
    for timestamp, row in frame.iterrows():
        usage = float(row["total"])
        production = float(row["production"])
        rows.append({
            "period": timestamp.strftime("%d-%m-%Y"),
            "usage": usage,
            "production": production,
            "from_grid": float(row["from_grid"]),
            "returned_to_grid": float(row["returned_to_grid"]),
            "self_consumed_percent": (min(usage, production) / production * 100) if production else 0.0,
        })
    return rows


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
            "change": ((total - previous_total) / previous_total * 100) if total > 0 and previous_total else None,
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
    contract_start_month: int = 8,
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

    daily_usage["contract_year"] = daily_usage.index.year - (daily_usage.index.month < contract_start_month).astype(int)
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
        contract_year = timestamp.year - int(timestamp.month < contract_start_month)
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
    """Summarize forecast totals for configured contract years."""
    if forecast.empty:
        return []
    frame = forecast.copy()
    frame["contract_year"] = frame.index.year - (frame.index.month < start_month).astype(int)
    end_month = start_month - 1 or 12
    end_year_offset = 0 if start_month == 1 else 1
    rows: list[dict[str, object]] = []
    for contract_year, group in frame.groupby("contract_year", sort=True):
        rows.append({
            "period": f"01-{start_month:02d}-{contract_year} t/m {pd.Timestamp(contract_year + end_year_offset, end_month, 1) + pd.offsets.MonthEnd(1):%d-%m-%Y}",
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
    contract_start_month: int = 8,
) -> pd.DataFrame:
    """Add optional annual electricity loads, distributed by quarter length."""
    adjusted = forecast.copy()
    annual_increment = (induction_kwh if induction else 0.0) + (heat_pump_kwh if heat_pump else 0.0)
    if adjusted.empty or not annual_increment:
        return adjusted
    contract_year = adjusted.index.year - (adjusted.index.month < contract_start_month).astype(int)
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


def contract_year_usage(
    readings: list[Reading],
    stream: str,
    contract_start_month: int = 8,
    timezone_name: str = "Europe/Amsterdam",
) -> float:
    """Return the latest configured contract-year usage."""
    frame = readings_frame(readings)
    if frame.empty:
        return 0.0
    frame.index = frame.index.tz_convert(timezone_name)
    if stream == "electricity":
        frame = frame[frame["stream"].isin(["elec_day", "elec_night"])]
    else:
        frame = frame[frame["stream"] == stream]
    daily = frame.groupby(frame.index.date)["value"].sum()
    if daily.empty:
        return 0.0
    daily.index = pd.DatetimeIndex(daily.index).tz_localize(timezone_name)
    contract_years = daily.index.year - (daily.index.month < contract_start_month).astype(int)
    return float(daily[contract_years == contract_years[-1]].sum())


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
            "change": ((total - previous) / previous * 100) if total > 0 and previous else None,
        })
        previous = total
    return rows


def solar_advice(annual_kwh: float, panel_wp: float = 455.0, yearly_yield_kwh: float | None = None, specific_yield_kwh_per_kwp: float = 880.0, roof_area_m2: float | None = None, panel_area_m2: float = 1.998, target_percent: float = 75.0, feed_in_tariff_eur_per_kwh: float = 0.0) -> dict[str, SolarAdvice]:
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
        # Under salderen, a feed-in tariff only applies to the annual surplus beyond usage, not to gross production.
        surplus = max(expected - annual_kwh, 0.0)
        advice[name] = SolarAdvice(count, count * panel_wp / 1000, expected, expected / annual_kwh * 100 if annual_kwh else 0.0, surplus, surplus * feed_in_tariff_eur_per_kwh)
    return advice
