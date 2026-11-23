# Placeholder for the optional local gateway-auth checkout (see the pip step).
FROM scratch AS gatewayauth

FROM python:3.12-slim

# Real system packages this backend genuinely needs beyond pip:
# - antiword / catdoc: legacy .doc/.xls/.ppt extraction (Doc Extractor
#   node — see requirements.txt's own note and GAP_REPORT §0.33)
# - libpq-dev / gcc: psycopg2-binary's real build dependencies
# - libcap2-bin: provides setcap, used below to grant `unshare` the
#   one real capability the Code node's own sandbox (sandbox.py)
#   genuinely needs, without making the whole app process root
# - git: pip resolves the gateway-auth requirement from a git tag
#   (requirements.txt), and python:3.12-slim ships no git at all — the
#   install failed with "git: not found" before this was added
RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword catdoc libpq-dev gcc libcap2-bin git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# requirements.txt pins the shared north-south trust boundary (gateway-auth) at a
# git tag — the same package DataConnect uses. That repo is private, so a read
# token arrives as a BuildKit secret and the URL is rewritten for this RUN only.
# The gitconfig holding the token is deleted in the SAME layer, so the token never
# persists in the image; the secret mount itself is never written to a layer.
#   docker build --secret id=gh_token,env=GH_TOKEN .
# Local builds can skip the token (same as the catalog backend): the dev
# docker-compose.override.yml passes a local checkout of
# InaiEra_dataplane_GatewayAuth as the `gatewayauth` build context, and the
# pinned git URL is resolved from that checkout instead of GitHub. Without it,
# `gatewayauth` is the empty stage above and the token path is used.
RUN --mount=type=secret,id=gh_token \
    --mount=type=bind,from=gatewayauth,target=/gatewayauth \
    if [ -d /gatewayauth/.git ]; then \
        git config --global --add safe.directory '*' \
     && git config --global url."file:///gatewayauth".insteadOf "https://github.com/AadhyaAnalyticsInc/InaiEra_dataplane_GatewayAuth.git"; \
    else \
        git config --global url."https://x-access-token:$(cat /run/secrets/gh_token)@github.com/".insteadOf "https://github.com/"; \
    fi \
 && pip install --no-cache-dir -r requirements.txt \
 && rm -f /root/.gitconfig

COPY app ./app

# Real, versioned schema migrations must be present INSIDE the image —
# app/db.py's own init_db() runs `alembic upgrade head` on every startup
# (AUTO_MIGRATE defaults to true) via _run_alembic_upgrade(), which
# resolves alembic.ini and migrations/ relative to this same /app dir.
# Without these two, a container start genuinely dies on a missing
# alembic.ini before it ever serves a request.
COPY alembic.ini .
COPY migrations ./migrations

# Real, non-root runtime user (§0.81, GAP_REPORT) — the actual fix for
# the reported gap: this image previously had no real USER directive
# at all, so every real process it runs — the API server, the worker,
# and (the report's own, correctly-named concern) every real
# subprocess a Code node's own sandbox (sandbox.py) or the legacy
# .doc/.xls/.ppt extraction tools (antiword/xls2csv/catppt,
# harness.py) genuinely spawn — ran as real, genuine root inside the
# container. A real container-escape or subprocess-sandbox-escape bug
# is a strictly worse real outcome as root than as an unprivileged
# user; this doesn't make either kind of bug impossible, but it
# removes a real, unnecessary amplification of one. `appuser` owns
# `/app` and `/var/inaiera-uploads` (storage.py's own real default
# LOCAL_STORAGE_DIR — created and chowned here, before the user switch
# below, since a real non-root user genuinely cannot create a
# directory under /var on its own) — the only two real,
# writable-by-this-app paths this image needs.
#
# A real, honest, DIRECT tension with sandbox.py's own real,
# documented constraint: creating the network/mount/PID namespaces the
# Code node's own sandbox relies on genuinely needs `CAP_SYS_ADMIN` —
# confirmed live in that module's own docstring by testing directly as
# an unprivileged user and getting a real `unshare failed: Operation
# not permitted`. Rather than solve this by making the ENTIRE app
# process root again (which would defeat the actual point of this
# fix), a real Linux file capability is granted to the one specific
# binary that needs it: `setcap cap_sys_admin+ep` on `unshare` itself,
# the real, standard least-privilege mechanism for exactly this case —
# `appuser` still owns none of that capability itself; only invoking
# `unshare` specifically gains it, for that one real call, not for
# anything else this process does. A real, honest caveat, not silently
# assumed to always work: some real container runtimes/kernels ignore
# file capabilities inside a container, or require the CONTAINER
# ITSELF to be granted `CAP_SYS_ADMIN` in its own capability set
# regardless of file capabilities on a binary inside it — see
# docker-compose.yml's own real `cap_add: [SYS_ADMIN]` on the api/
# worker services for that real, additional, honest requirement.
# sandbox.py's own `run_sandboxed_code()` already checks, live, on
# every call, whether real isolation can actually be created and
# refuses to execute at all if it can't — this genuinely fails closed
# in a real deployment where neither mechanism is available, rather
# than silently downgrading to weaker guarantees.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 1000 appuser \
    && mkdir -p /var/inaiera-uploads \
    && chown -R appuser:appuser /app /var/inaiera-uploads \
    && setcap cap_sys_admin+ep "$(command -v unshare)"
USER appuser

# Real default: the API server. The worker service in docker-compose.yml
# overrides this with `python -m app.worker` — one real image, two real
# running modes, matching how this backend already works when run
# directly on a host (same app/ package, two different entrypoints).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
