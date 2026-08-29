FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml requirements.lock ./
RUN python -m pip install --upgrade pip && \
    python -m pip install --requirement requirements.lock

COPY README.md ./
COPY backend ./backend
COPY alembic.ini ./
RUN python -m pip install --no-deps .

RUN useradd --create-home --uid 10001 appuser && \
    install -d -o appuser -g appuser /data
USER appuser

EXPOSE 8020 8023
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8020"]
