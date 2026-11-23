# inaiera-data-quality FastAPI service
# Build: docker build -f docker/Dockerfile -t inaiera-data-quality:latest .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[prod]"

# app/ includes app/contracts/quality_contract.json (shared quality contract).
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini .

RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER 10001

EXPOSE 8086
# Migrations: Helm runs them once in a pre-install/pre-upgrade Job and sets
# RUN_MIGRATIONS_ON_START=false. Local compose keeps the default (true) so a
# new migration reaches its database on `up`. Startup itself fails fast when
# an enabled integration is misconfigured (DQ-CFG-001).
CMD ["sh", "-c", "if [ \"${RUN_MIGRATIONS_ON_START:-true}\" = \"true\" ]; then alembic upgrade head || exit 1; fi; exec uvicorn app.main:app --host 0.0.0.0 --port 8086"]
