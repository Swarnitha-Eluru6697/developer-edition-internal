# Placeholder for the optional local gateway-auth checkout (see the pip install step).
FROM scratch AS gatewayauth

FROM python:3.11.15-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

WORKDIR /app

# git is needed to resolve the pinned gateway-auth dependency in pyproject.toml.
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# README.md is declared as the package readme (pyproject.toml: readme = "README.md"), so the editable install below needs it next
# to pyproject.toml — copy both before `pip install`.
# Build tooling is upgraded FIRST, with floors, because the base image's pip/setuptools can lag behind published fixes
# (pip-audit: PYSEC-2026-196/3721 for pip, PYSEC-2026-3447 for setuptools). The floors are checked by tests/test_supply_chain_config.py.
RUN pip install --no-cache-dir --upgrade "pip>=26.2" "setuptools>=83.0.0"

COPY pyproject.toml README.md ./
# The permission catalog (contract section 7): app/core/permissions.py loads it at startup.
COPY permissions.json ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

# pyproject.toml pins the shared north-south trust boundary (gateway-auth) at a
# git tag — the same package UCS uses. That repo is private, so a read token
# arrives as a BuildKit secret and the URL is rewritten for this RUN only. The
# gitconfig holding the token is deleted in the SAME layer, so the token never
# persists in the image; the secret mount itself is never written to a layer.
#   docker build --secret id=gh_token,env=GH_TOKEN -t dataconnect-backend .
#
# Local builds can skip the token (same as UCS): pass a checkout of
# InaiEra_dataplane_GatewayAuth that has the pinned tag as the `gatewayauth` build
# context and the git URL resolves from it. Without it, `gatewayauth` is the empty
# stage at the top of this file and the token path is used (CI / release builds).
RUN --mount=type=secret,id=gh_token \
    --mount=type=bind,from=gatewayauth,target=/gatewayauth \
    if [ -d /gatewayauth/.git ]; then \
        git config --global --add safe.directory '*' \
     && git config --global url."file:///gatewayauth".insteadOf "https://github.com/AadhyaAnalyticsInc/InaiEra_dataplane_GatewayAuth.git"; \
    else \
        git config --global url."https://x-access-token:$(cat /run/secrets/gh_token)@github.com/".insteadOf "https://github.com/"; \
    fi \
 && pip install --no-cache-dir -e . \
 && rm -f /root/.gitconfig

EXPOSE 8000

# The release/startup contract (validate config -> verify Postgres -> run
# migrations -> verify the seed/template version -> exec uvicorn) — see
# app/startup.py for why this replaced starting uvicorn directly: nothing
# here previously ensured migrations had actually been applied before the
# API started accepting traffic.
CMD ["python", "-m", "app.startup"]
