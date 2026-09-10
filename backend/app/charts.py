from __future__ import annotations

import io
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter
import pandas as pd


def render_series_chart(frame: pd.DataFrame, stream: str, granularity: str) -> bytes:
    figure, axis = plt.subplots(figsize=(11, 4.5))
    figure.patch.set_facecolor("#f7f3eb")
    axis.set_facecolor("#fffdf8")
    if frame.empty:
        axis.text(0.5, 0.5, "No readings in this window", ha="center", va="center", color="#5a625d", transform=axis.transAxes)
    elif stream == "electricity":
        width = _bar_width(frame.index)
        axis.bar(frame.index, frame["elec_night"], label="Night", color="#1e5360", width=width)
        axis.bar(frame.index, frame["elec_day"], bottom=frame["elec_night"], label="Day", color="#e7a943", width=width)
        axis.set_ylabel("kWh")
        figure.legend(frameon=False, ncols=2, loc="lower center", bbox_to_anchor=(0.5, 0.02))
    else:
        axis.bar(frame.index, frame["gas"], color="#d96b4d", width=_bar_width(frame.index), label="Gas")
        axis.set_ylabel("m³")
        figure.legend(frameon=False, loc="lower center", bbox_to_anchor=(0.5, 0.02))
    axis.grid(axis="y", color="#d8d5ca", linewidth=0.7)
    axis.spines[["top", "right"]].set_visible(False)
    _format_time_axis(axis, granularity, frame.index.tz if isinstance(frame.index, pd.DatetimeIndex) else None)
    axis.set_title("Electricity usage" if stream == "electricity" else "Gas usage", loc="left", weight="bold")
    figure.subplots_adjust(bottom=0.22)
    output = io.BytesIO()
    figure.savefig(output, format="svg", transparent=False)
    plt.close(figure)
    return output.getvalue()


def render_offset_chart(frame: pd.DataFrame, granularity: str) -> bytes:
    """Usage (day/night bars) vs. modeled solar production, with the surplus returned to the grid."""
    figure, axis = plt.subplots(figsize=(11, 4.5))
    figure.patch.set_facecolor("#f7f3eb")
    axis.set_facecolor("#fffdf8")
    if frame.empty:
        axis.text(0.5, 0.5, "No readings in this window", ha="center", va="center", color="#5a625d", transform=axis.transAxes)
    else:
        width = _bar_width(frame.index)
        axis.bar(frame.index, frame["elec_night"], label="Night", color="#1e5360", width=width)
        axis.bar(frame.index, frame["elec_day"], bottom=frame["elec_night"], label="Day", color="#e7a943", width=width)
        axis.bar(frame.index, -frame["returned_to_grid"], label="Returned to grid", color="#3f9142", width=width)
        axis.plot(frame.index, frame["production"], label="Modeled solar production", color="#7a4fae", linewidth=1.6, linestyle="--")
        axis.axhline(0, color="#5a625d", linewidth=0.8)
        axis.set_ylabel("kWh")
        figure.legend(frameon=False, ncols=4, loc="lower center", bbox_to_anchor=(0.5, 0.02))
    axis.grid(axis="y", color="#d8d5ca", linewidth=0.7)
    axis.spines[["top", "right"]].set_visible(False)
    _format_time_axis(axis, granularity, frame.index.tz if isinstance(frame.index, pd.DatetimeIndex) else None)
    axis.set_title("Electricity usage offset by modeled solar production", loc="left", weight="bold")
    figure.subplots_adjust(bottom=0.22)
    output = io.BytesIO()
    figure.savefig(output, format="svg", transparent=False)
    plt.close(figure)
    return output.getvalue()


def render_forecast_chart(
    frame: pd.DataFrame,
    electricity_base: float = 36810.0,
    gas_base: float = 6183.0,
    forecast_start: pd.Timestamp | None = None,
) -> bytes:
    figure, electricity_axis = plt.subplots(figsize=(11, 4.5))
    figure.patch.set_facecolor("#f7f3eb")
    electricity_axis.set_facecolor("#fffdf8")
    if frame.empty:
        electricity_axis.text(0.5, 0.5, "Not enough readings for a forecast", ha="center", va="center", color="#5a625d", transform=electricity_axis.transAxes)
    else:
        cumulative = frame[["electricity", "gas"]].cumsum()
        cumulative["electricity"] += electricity_base
        cumulative["gas"] += gas_base
        baseline_index = pd.DatetimeIndex([forecast_start or frame.index[0]])
        quarter_end_index = pd.DatetimeIndex([
            timestamp + pd.offsets.QuarterEnd(startingMonth=1)
            for timestamp in frame.index
        ])
        plot_index = baseline_index.append(quarter_end_index)
        plot_values = pd.concat(
            [
                pd.DataFrame({"electricity": [electricity_base], "gas": [gas_base]}, index=baseline_index),
                cumulative,
            ]
        )
        gas_axis = electricity_axis.twinx()
        electricity_axis.plot(plot_index, plot_values["electricity"], marker="o", linewidth=2.2, color="#e7a943", label="Cumulative electricity forecast")
        gas_axis.plot(plot_index, plot_values["gas"], marker="o", linewidth=2.2, color="#d96b4d", label="Cumulative gas forecast")
        electricity_axis.annotate(
            f"{plot_values['electricity'].iloc[-1]:,.0f} kWh",
            xy=(plot_index[-1], plot_values["electricity"].iloc[-1]),
            xytext=(-8, 8),
            textcoords="offset points",
            ha="right",
            color="#9a6b18",
            fontsize=9,
            fontweight="bold",
        )
        gas_axis.annotate(
            f"{plot_values['gas'].iloc[-1]:,.0f} m³",
            xy=(plot_index[-1], plot_values["gas"].iloc[-1]),
            xytext=(-8, -14),
            textcoords="offset points",
            ha="right",
            color="#a44b37",
            fontsize=9,
            fontweight="bold",
        )
        electricity_axis.set_ylabel("Cumulative electricity kWh")
        gas_axis.set_ylabel("Cumulative gas m³")
        handles, labels = electricity_axis.get_legend_handles_labels()
        gas_handles, gas_labels = gas_axis.get_legend_handles_labels()
        figure.legend(handles + gas_handles, labels + gas_labels, frameon=False, ncols=2, loc="lower center", bbox_to_anchor=(0.5, 0.02))
        electricity_axis.xaxis.set_major_formatter(FuncFormatter(_quarter_label))
    electricity_axis.grid(axis="y", color="#d8d5ca", linewidth=0.7)
    electricity_axis.spines[["top", "right"]].set_visible(False)
    electricity_axis.set_title("Two-year cumulative energy forecast", loc="left", weight="bold")
    figure.subplots_adjust(bottom=0.22)
    output = io.BytesIO()
    figure.savefig(output, format="svg", transparent=False)
    plt.close(figure)
    return output.getvalue()


def _quarter_label(value: float, _position: float) -> str:
    timestamp = pd.Timestamp(value, unit="D", tz="UTC")
    return f"Q{timestamp.quarter} {timestamp.year}"


def _format_time_axis(axis: plt.Axes, granularity: str, timezone_info: object) -> None:
    timezone = timezone_info or ZoneInfo("Europe/Amsterdam")
    axis.xaxis.set_major_formatter(FuncFormatter(lambda value, position: _dutch_time_label(value, position, granularity, timezone)))
    axis.tick_params(axis="x", rotation=0, labelsize=8)


def _dutch_time_label(value: float, _position: float, granularity: str, timezone: object) -> str:
    timestamp = mdates.num2date(value, tz=timezone)
    if granularity == "hour":
        return timestamp.strftime("%H:%M")
    if granularity == "day":
        weekdays = ("ma", "di", "wo", "do", "vr", "za", "zo")
        return f"{weekdays[timestamp.weekday()]}\n{timestamp:%d-%m}"
    months = ("januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december")
    return f"{months[timestamp.month - 1]} {timestamp.year}"


def _bar_width(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 0.03
    spacing = (index[1] - index[0]).total_seconds() / 86400
    return max(spacing * 0.7, 0.01)
