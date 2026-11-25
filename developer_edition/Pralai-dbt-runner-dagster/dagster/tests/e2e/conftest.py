"""Production-topology tests (DG-021): they talk to a RUNNING stack (Dagster webserver + daemon + code server + dbt Runner + Postgres).

Opt-in: nothing here runs unless DAGSTER_E2E=1, because they start real dbt runs and change a project's schedule (restored afterwards).

    DAGSTER_E2E=1 DAGSTER_URL=http://localhost:3001 DBT_RUNNER_URL=http://localhost:8082 \\
    E2E_DELEGATION_SECRET=... E2E_ORG_ID=local-dev E2E_RUNNER_DB_CONTAINER=dbtrunner-postgres-1 \\
    E2E_CODE_CONTAINER=dagster-dagster-code-1  python -m pytest tests/e2e -v

Needs `httpx`, `python-jose` and (for container checks / cleanup) the `docker` CLI on the machine running the tests.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
import uuid

import httpx
import pytest

ENABLED = os.environ.get("DAGSTER_E2E") == "1"
DAGSTER = os.environ.get("DAGSTER_URL", "http://localhost:3001")
RUNNER = os.environ.get("DBT_RUNNER_URL", "http://localhost:8082")
DELEGATION_SECRET = os.environ.get("E2E_DELEGATION_SECRET", "local-dev-dbtrunner-delegation-secret")
ORG_ID = os.environ.get("E2E_ORG_ID", "local-dev")
RUNNER_DB = os.environ.get("E2E_RUNNER_DB_CONTAINER", "dbtrunner-postgres-1")
CODE_CONTAINER = os.environ.get("E2E_CODE_CONTAINER", "dagster-dagster-code-1")

pytestmark = pytest.mark.skipif(not ENABLED, reason="production-topology tests are opt-in: set DAGSTER_E2E=1")


def delegated_token(scope: str, roles=("data_transformer",)) -> str:
    """A fresh single-use Workspace-style delegated token (the runner refuses a replay)."""
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=")
    now = int(time.time())
    head = b64({"alg": "HS256", "typ": "JWT"})
    body = b64({"iss": "workspace", "act": {"sub": "workspace"}, "sub": "e2e", "aud": "dbtrunner", "scope": scope, "org_id": ORG_ID,
                "roles": list(roles), "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60})
    sig = base64.urlsafe_b64encode(hmac.new(DELEGATION_SECRET.encode(), head + b"." + body, hashlib.sha256).digest()).rstrip(b"=")
    return (head + b"." + body + b"." + sig).decode()


class Runner:
    def call(self, method: str, path: str, scope: str, body=None):
        r = httpx.request(method, f"{RUNNER}/api/v1{path}", json=body, timeout=60,
                          headers={"Authorization": f"Bearer {delegated_token(scope)}"})
        return r

    def projects(self):
        r = self.call("GET", "/projects", "dbt.projects.read")
        r.raise_for_status()
        return r.json()


def sql(query: str) -> str:
    if not shutil.which("docker"):
        pytest.skip("docker CLI not available")
    return subprocess.check_output(["docker", "exec", RUNNER_DB, "psql", "-U", "dbtrunner", "-d", "dbtrunner", "-Atc", query], text=True).strip()


def graphql(query: str) -> dict:
    r = httpx.post(f"{DAGSTER}/graphql", json={"query": query}, timeout=60)
    r.raise_for_status()
    body = r.json()
    assert "errors" not in body, body.get("errors")
    return body["data"]


@pytest.fixture(scope="session")
def runner() -> Runner:
    return Runner()


@pytest.fixture(scope="session")
def locations() -> dict:
    """name -> {jobs: [...], sensors: {name: status}} for every location the webserver has loaded."""
    data = graphql("""{ workspaceOrError { __typename ... on Workspace { locationEntries { name loadStatus locationOrLoadError {
        __typename ... on RepositoryLocation { repositories { jobs { name } sensors { name sensorState { status } } } }
        ... on PythonError { message } } } } } }""")
    out = {}
    for e in data["workspaceOrError"]["locationEntries"]:
        assert e["loadStatus"] == "LOADED" and e["locationOrLoadError"]["__typename"] == "RepositoryLocation", e
        repo = e["locationOrLoadError"]["repositories"][0]
        out[e["name"]] = {"jobs": sorted(j["name"] for j in repo["jobs"]), "sensors": {s["name"]: s["sensorState"]["status"] for s in repo["sensors"]}}
    return out
