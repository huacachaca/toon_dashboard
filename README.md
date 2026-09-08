# Toon Energy Dashboard

A local FastAPI dashboard for Toon energy exports. It imports electricity day/night and gas readings, stores them in SQLite, shows historical usage charts, forecasts the next eight quarters, and calculates solar-panel advice. Docker Compose runs the dashboard with HTTPS and persistent storage.

## Before Use

Replace the placeholders in `docker.env.example` and the Docker files:

- `<<ipaddress>>`: the Toon address reachable from the container host.
- `<<certificate>>`: the host path to the TLS certificate, and the matching container path.
- `<<certificate_key>>`: the host path to the TLS private key, and the matching container path.
- `<<port>>`: the HTTPS port to publish.

Set `TOON_FORECAST_ELECTRICITY_TN_BASE`, `TOON_FORECAST_ELECTRICITY_TL_BASE`, and `TOON_FORECAST_GAS_BASE` to the current meter-counter values. They intentionally default to `0` in this repository. Set the optional induction and heat-pump forecast loads if needed.

Set `TOON_SOLAR_TARGET_PERCENT` to choose the percentage of annual electricity usage used for the Solar advice panel-count calculation. The Solar advice cog can also change this value while the dashboard is running.

The Solar advice cog also accepts a panel name and panel power. Leave the yearly yield field blank to use the existing `880 kWh/kWp/year` factor, which calculates `455 Wp × 880 / 1000 = 400.4 kWh/year` for the default panel. Enter a yearly yield when the supplier provides a fixed per-panel value; that value takes precedence over the factor.

The Forecast settings cog also accepts the date on which those meter baselines apply. The graph starts at the beginning of the next quarter from the current date and adds stored electricity and gas usage from the selected base date through that forecast start.

Copy the edited environment file to `.env`, then build and start the container:

```sh
cp docker.env.example .env
docker-compose up -d --build
```

Before refreshing data, generate an export in the Toon interface. The application can download only an export that Toon has made available on the local network. SQLite data and archived exports are stored in the Docker volume `toon-energy-data`.

For local development, run from `backend/` with `uvicorn app.main:app --reload`; this uses HTTP on port `8000` and does not require the Docker TLS files.
