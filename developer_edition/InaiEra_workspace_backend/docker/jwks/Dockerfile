# Dev-only control-plane stub: serves JWKS on :9999 and mints the dev JWT that
# workspace / dbt-runner / flinksql-runner / data-quality all validate against.
#
# The RSA key and minted token live in a bind mount at ~/.inaiera-dev-jwks so the
# token stays stable across restarts AND stays readable from the host — the
# workspace SPA's Vite plugin reads token.txt from that exact path to redirect
# / -> /#token=<jwt>. Do not relocate it without changing the Vite config too.
FROM python:3.12-slim

RUN pip install --no-cache-dir cryptography

# Baked in so the stack builds on a machine that has no ~/.inaiera-dev-jwks yet;
# the compose mount overlays only the key directory, not this script.
COPY jwks_server.py /srv/jwks_server.py

ENV HOME=/root PYTHONUNBUFFERED=1
EXPOSE 9999
CMD ["python", "/srv/jwks_server.py"]
