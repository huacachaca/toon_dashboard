from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .store import Store
from .web import router


def create_app() -> FastAPI:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    application = FastAPI(title="Toon Energy Dashboard")
    application.state.settings = settings
    application.state.store = Store(settings.database_path, settings.timezone)
    static_directory = Path(__file__).parent / "static"
    application.mount("/static", StaticFiles(directory=static_directory), name="static")
    application.include_router(router)
    return application


app = create_app()
