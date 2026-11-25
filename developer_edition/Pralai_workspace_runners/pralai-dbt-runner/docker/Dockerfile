# Multi-stage build for the InAiEra dbt Runner service.
#
# Targets:
#   runtime (default) — the API service. Also contains the isolated run-Job
#                       entrypoint, so by default the chart runs Jobs from this
#                       same image (runner.image empty -> image.repository:tag).
#   job               — optional dedicated image for the per-run Kubernetes Job
#                       (same code; entrypoint = app.executors.job_entrypoint).
#
#   docker build -f docker/Dockerfile -t registry.inaiera.com/products/dbt-runner:0.1.0 .
#   docker build -f docker/Dockerfile --target job \
#       -t registry.inaiera.com/products/dbt-runner-job:0.1.0 .
#   # then: helm ... --set runner.image=registry.inaiera.com/products/dbt-runner-job:0.1.0

FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

# ---------- Shared runtime base ----------
FROM python:3.11-slim AS base

# git: GitService shells out via gitpython to clone registered projects.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git libpq5 ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r dbtrunner --gid 10001 \
    && useradd -r -g dbtrunner --uid 10001 --home-dir /home/dbtrunner --shell /bin/false dbtrunner \
    && mkdir -p /var/lib/dbtrunner/git /home/dbtrunner \
    && chown -R dbtrunner:dbtrunner /var/lib/dbtrunner /home/dbtrunner

COPY --from=builder /opt/venv /opt/venv
COPY --chown=dbtrunner:dbtrunner app /app/app
COPY --chown=dbtrunner:dbtrunner migrations /app/migrations
COPY --chown=dbtrunner:dbtrunner alembic.ini /app/alembic.ini

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_WORKDIR=/var/lib/dbtrunner/git

USER dbtrunner
WORKDIR /app

# ---------- Isolated run Job (optional dedicated image) ----------
# Same code and dependencies as the API image; no HTTP server, no healthcheck.
# The Job spec also sets `command` explicitly, so either image works.
FROM base AS job
ENV HOME=/tmp
ENTRYPOINT ["tini", "--", "python", "-m", "app.executors.job_entrypoint"]
CMD []

# ---------- API service (default target: must stay LAST) ----------
FROM base AS runtime

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://localhost:8080/readyz', timeout=3).status==200 else 1)"

ENTRYPOINT ["tini", "--"]
# Two workers are safe: the scheduler / run reconciler / metadata outbox loops
# are leader-elected (Postgres advisory lock, app/services/leader.py), so they
# run in exactly one process across all workers AND replicas.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "2", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--no-access-log"]

