from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .analysis import apply_forecast_electricity_adjustments, analysis_rows, annualized_usage, forecast_contract_year_rows, forecast_series, gas_analysis_rows, next_quarter_start, series, solar_advice, usage_between, zoom_window
from .charts import render_forecast_chart, render_series_chart
from .config import Settings
from .refresh import refresh_data
from .store import Store
from .toon_client import ToonDownloadError


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
logger = logging.getLogger(__name__)


def _context(request: Request, settings: Settings, store: Store, **values: object) -> dict[str, object]:
    first, last, count = store.coverage()
    context = {
        "request": request,
        "settings": settings,
        "coverage_start": first,
        "coverage_end": last,
        "reading_count": count,
        "recent_exports": store.recent_exports(),
        "stream": "electricity",
        "selected_date": date.today(),
        "zoom": "day",
        "message": None,
        "error": None,
    }
    context.update(values)
    return context


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    stream: Annotated[str, Query(pattern="^(electricity|gas|forecast)$")] = "electricity",
    selected_date: date | None = None,
    zoom: Annotated[str, Query(pattern="^(day|week|month|year)$")] = "day",
    induction: bool = False,
    heat_pump: bool = False,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    store: Store = request.app.state.store
    chosen_date = selected_date or date.today()
    readings = store.all_readings()
    forecast_start = next_quarter_start(datetime.now(), settings.timezone)
    forecast_data = forecast_series(readings, settings.timezone, forecast_start=forecast_start) if stream == "forecast" else None
    if forecast_data is not None:
        forecast_data = apply_forecast_electricity_adjustments(
            forecast_data,
            induction=induction,
            heat_pump=heat_pump,
            induction_kwh=settings.forecast_induction_kwh,
            heat_pump_kwh=settings.forecast_heat_pump_kwh,
        )
    forecast_year_rows = forecast_contract_year_rows(forecast_data) if forecast_data is not None else []
    if stream == "forecast":
        chart_data = None
        rows = []
        annual_total = float(forecast_data["electricity"].sum() / 2) if not forecast_data.empty else 0.0
    else:
        window_start, window_end, granularity = zoom_window(chosen_date, zoom, settings.timezone)
        chart_data = series(readings, stream, granularity, window_start, window_end, settings.timezone)
        rows = analysis_rows(readings, chosen_date.year, timezone_name=settings.timezone) if stream == "electricity" else gas_analysis_rows(readings, chosen_date.year, settings.timezone)
        annual_total = annualized_usage(readings, "electricity", settings.timezone)
    has_solar_basis = bool(rows) or (stream == "forecast" and forecast_data is not None and not forecast_data.empty)
    advice = solar_advice(
        annual_total,
        panel_wp=settings.panel_wp,
        yearly_yield_kwh=settings.solar_panel_yearly_yield_kwh,
        specific_yield_kwh_per_kwp=settings.specific_yield_kwh_per_kwp,
        panel_area_m2=settings.panel_area_m2,
        target_percent=settings.solar_target_percent,
    ) if has_solar_basis else {}
    forecast_solar_advice = [
        solar_advice(
            float(row["electricity"]),
            panel_wp=settings.panel_wp,
            yearly_yield_kwh=settings.solar_panel_yearly_yield_kwh,
            specific_yield_kwh_per_kwp=settings.specific_yield_kwh_per_kwp,
            panel_area_m2=settings.panel_area_m2,
            target_percent=settings.solar_target_percent,
        )
        for row in forecast_year_rows
    ] if stream == "forecast" else []
    forecast_solar_rows = list(zip(forecast_year_rows, forecast_solar_advice))
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=_context(request, settings, store, stream=stream, selected_date=chosen_date, zoom=zoom, induction=induction, heat_pump=heat_pump, forecast_start=forecast_start, chart_data=chart_data, forecast_data=forecast_data, forecast_year_rows=forecast_year_rows, forecast_solar_rows=forecast_solar_rows, analysis_rows=rows, solar_advice=advice),
    )


@router.post("/refresh", response_class=HTMLResponse)
def refresh(request: Request) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    store: Store = request.app.state.store
    try:
        result = refresh_data(settings, store)
        return RedirectResponse(url=f"/?message=Imported+{result.imported_rows}+readings", status_code=303)
    except ToonDownloadError as error:
        return RedirectResponse(url=f"/?error={str(error)}", status_code=303)
    except Exception:
        logger.exception("Toon export refresh failed")
        return RedirectResponse(url="/?error=The export could not be parsed. Check the application log.", status_code=303)


@router.post("/forecast-settings", response_class=HTMLResponse)
def forecast_settings(
    request: Request,
    electricity_tn_base: Annotated[float, Form(ge=0)] = 0.0,
    electricity_tl_base: Annotated[float, Form(ge=0)] = 0.0,
    gas_base: Annotated[float, Form(ge=0)] = 0.0,
    forecast_base_date: Annotated[date, Form()] = date(2026, 8, 1),
) -> RedirectResponse:
    settings: Settings = request.app.state.settings
    settings.forecast_electricity_tn_base = electricity_tn_base
    settings.forecast_electricity_tl_base = electricity_tl_base
    settings.forecast_gas_base = gas_base
    settings.forecast_base_date = forecast_base_date
    return RedirectResponse(url="/?stream=forecast&message=Forecast+base+values+updated", status_code=303)


@router.post("/solar-settings", response_class=HTMLResponse)
def solar_settings(
    request: Request,
    solar_target_percent: Annotated[float, Form(ge=0, le=100)] = 75.0,
    solar_panel_name: Annotated[str, Form(min_length=1, max_length=120)] = "JA Solar JAM54D41-455/LB",
    panel_wp: Annotated[float, Form(gt=0)] = 455.0,
    solar_panel_yearly_yield_kwh: Annotated[str, Form()] = "",
    stream: Annotated[str, Form()] = "electricity",
) -> RedirectResponse:
    settings: Settings = request.app.state.settings
    settings.solar_target_percent = solar_target_percent
    settings.solar_panel_name = solar_panel_name.strip()
    settings.panel_wp = panel_wp
    try:
        yearly_yield = float(solar_panel_yearly_yield_kwh) if solar_panel_yearly_yield_kwh.strip() else None
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Yearly panel yield must be a positive number or blank") from error
    if yearly_yield is not None and yearly_yield <= 0:
        raise HTTPException(status_code=422, detail="Yearly panel yield must be positive")
    settings.solar_panel_yearly_yield_kwh = yearly_yield
    return RedirectResponse(url=f"/?stream={stream}&message=Solar+coverage+updated", status_code=303)


@router.get("/status", response_class=HTMLResponse)
def status(request: Request) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    store: Store = request.app.state.store
    return templates.TemplateResponse(request=request, name="status.html", context=_context(request, settings, store))


@router.get("/series")
def chart(
    request: Request,
    stream: Annotated[str, Query(pattern="^(electricity|gas|forecast)$")] = "electricity",
    selected_date: date | None = None,
    zoom: Annotated[str, Query(pattern="^(day|week|month|year)$")] = "day",
    induction: bool = False,
    heat_pump: bool = False,
) -> Response:
    settings: Settings = request.app.state.settings
    store: Store = request.app.state.store
    chosen_date = selected_date or date.today()
    if stream == "forecast":
        forecast_start = next_quarter_start(datetime.now(), settings.timezone)
        readings = store.all_readings()
        forecast_data = forecast_series(readings, settings.timezone, forecast_start=forecast_start)
        accumulated = usage_between(readings, settings.forecast_base_date, forecast_start, settings.timezone)
        forecast_data = apply_forecast_electricity_adjustments(
            forecast_data,
            induction=induction,
            heat_pump=heat_pump,
            induction_kwh=settings.forecast_induction_kwh,
            heat_pump_kwh=settings.forecast_heat_pump_kwh,
        )
        return Response(
            render_forecast_chart(
                forecast_data,
                electricity_base=settings.forecast_electricity_tn_base + settings.forecast_electricity_tl_base + accumulated["electricity"],
                gas_base=settings.forecast_gas_base + accumulated["gas"],
                forecast_start=forecast_start,
            ),
            media_type="image/svg+xml",
        )
    window_start, window_end, granularity = zoom_window(chosen_date, zoom, settings.timezone)
    chart_data = series(store.all_readings(), stream, granularity, window_start, window_end, settings.timezone)
    return Response(render_series_chart(chart_data, stream, granularity), media_type="image/svg+xml")
