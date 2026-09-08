FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TOON_DATA_DIR=/app/data

WORKDIR /app/backend

COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY backend/app /app/backend/app

RUN pip install --no-cache-dir /app/backend

RUN mkdir -p /app/data

EXPOSE <<port>>

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import ssl, urllib.request; urllib.request.urlopen('https://127.0.0.1:<<port>>/status', context=ssl._create_unverified_context(), timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "<<port>>", "--ssl-certfile", "<<certificate>>", "--ssl-keyfile", "<<certificate_key>>"]
