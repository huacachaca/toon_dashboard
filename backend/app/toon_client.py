from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx


class ToonDownloadError(RuntimeError):
    """Raised when the Toon export cannot be downloaded."""


def download_export(toon_host: str, raw_dir: Path, timeout_seconds: float = 30.0) -> tuple[bytes, Path]:
    """Download the currently available export and atomically archive it."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    url = toon_host if toon_host.startswith("http") else f"http://{toon_host}"
    url = f"{url.rstrip('/')}/export.zip"
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise ToonDownloadError("No export is available. Open the export page and generate one on the Toon first.") from error
        raise ToonDownloadError(f"Toon returned HTTP {error.response.status_code}.") from error
    except httpx.HTTPError as error:
        raise ToonDownloadError("The Toon could not be reached. Check that this computer is on the same LAN.") from error

    if response.headers.get("content-type", "").split(";", 1)[0].lower() != "application/zip":
        raise ToonDownloadError("The Toon response was not a ZIP export.")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = raw_dir / f"export-{timestamp}.zip"
    fd, temporary_name = tempfile.mkstemp(prefix=".export-", suffix=".tmp", dir=raw_dir)
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(response.content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return response.content, destination
