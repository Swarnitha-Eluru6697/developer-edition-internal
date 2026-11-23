# Multi-stage build for the InAiEra Workspace service.
#
# Stage 1 builds Python dependencies into a wheel cache.
# Stage 2 produces the runtime image, minimal and non-root.

# Placeholder for the optional local gateway-auth checkout (see below).
FROM scratch AS gatewayauth

FROM python:3.11-slim AS builder

WORKDIR /build

# Build deps for native packages (psycopg2, cryptography, gitpython links to
# git) — git is also needed here (not just at runtime) to resolve the
# inaiera-gateway-auth git dependency below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# Resolve and install runtime deps into a venv we'll copy into the final image.
# inaiera-gateway-auth (pyproject.toml) resolves from a private repo, so the
# install needs a GitHub read credential — passed as a BuildKit secret so it
# never persists in an image layer or its history:
#   docker build --secret id=gh_token,env=GH_TOKEN -f docker/Dockerfile .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip
# Local builds can skip the token: the dev compose files pass a local checkout
# of InaiEra_dataplane_GatewayAuth as the `gatewayauth` build context, and the
# pinned git URL is then resolved from that checkout instead
# of GitHub (it must have the pinned tag, v1.1.0-rc.1). Without it, `gatewayauth` is the empty stage below and the token
# path is used, as in CI / release builds.
RUN --mount=type=secret,id=gh_token \
    --mount=type=bind,from=gatewayauth,target=/gatewayauth \
    if [ -d /gatewayauth/.git ]; then \
        git config --global --add safe.directory '*' \
     && git config --global url."file:///gatewayauth".insteadOf "https://github.com/AadhyaAnalyticsInc/InaiEra_dataplane_GatewayAuth.git"; \
    else \
        git config --global url."https://x-access-token:$(cat /run/secrets/gh_token)@github.com/".insteadOf "https://github.com/"; \
    fi \
 && /opt/venv/bin/pip install --no-cache-dir . \
 && rm -f /root/.gitconfig

# ---------- Runtime ----------
FROM python:3.11-slim AS runtime

# git is required for the LocalRepo operations (gitpython shells out)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git libpq5 ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r workspace --gid 10001 \
    && useradd -r -g workspace --uid 10001 --home-dir /home/workspace --shell /bin/false workspace \
    && mkdir -p /var/lib/workspace/git /home/workspace \
    && chown -R workspace:workspace /var/lib/workspace /home/workspace

# Own the venv so notebook cells can `pip install` packages at runtime (the
# kernel runs as the non-root `workspace` user). Runtime-installed packages
# live in the container's writable layer — add long-term deps to pyproject.toml.
COPY --from=builder --chown=workspace:workspace /opt/venv /opt/venv
COPY --chown=workspace:workspace app /app/app
COPY --chown=workspace:workspace migrations /app/migrations
COPY --chown=workspace:workspace alembic.ini /app/alembic.ini
# The published permission IDs (contract section 7): app/core/rbac.py loads them at start-up.
COPY --chown=workspace:workspace permissions.json /app/permissions.json
COPY --chown=workspace:workspace docker/git-credential-helper.sh /usr/local/bin/git-credential-helper.sh
RUN chmod +x /usr/local/bin/git-credential-helper.sh

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORKSPACE_GIT_ROOT=/var/lib/workspace/git \
    HOME=/home/workspace \
    # Writable dirs for the local IPython kernel's connection/runtime files.
    JUPYTER_RUNTIME_DIR=/tmp/jupyter/runtime \
    JUPYTER_DATA_DIR=/tmp/jupyter/data \
    IPYTHONDIR=/tmp/ipython

USER workspace
WORKDIR /app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://localhost:8080/readyz', timeout=3).status==200 else 1)"

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "4", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--no-access-log"]
