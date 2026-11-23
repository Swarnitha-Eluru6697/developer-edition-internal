"""DG-003 (local stack): only the code server holds Dagster's identity at the dbt Runner.

The three Dagster services used to share one `env_file: .env`, so the webserver and the daemon carried the key that signs
Dagster's scoped runner tokens (project enumeration, run triggering) although sensors and runs execute inside the code server.
"""
from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "dagster" / "docker-compose.yml"
RUNNER = {"DBT_RUNNER_BASE_URL", "SCHEDULER_JWT_SECRET", "ORG_ID"}


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def test_only_the_code_server_gets_the_runner_identity():
    svc = _services()
    assert RUNNER <= set(svc["dagster-code"]["environment"])
    for name in ("dagster-daemon", "dagster-webserver", "dagster-db"):
        assert not RUNNER & set(svc[name].get("environment", {})), name


def test_no_service_loads_a_whole_env_file_again():
    """env_file would hand every key of .env (including the signing key) to the service it is on."""
    for name, s in _services().items():
        assert "env_file" not in s, name


def test_the_runner_values_are_required_for_the_code_server_and_never_defaulted():
    env = _services()["dagster-code"]["environment"]
    for key in RUNNER:
        assert str(env[key]).startswith("${" + key + ":?"), key          # compose refuses to start without them


def test_every_service_still_gets_what_dagster_itself_needs():
    for name in ("dagster-code", "dagster-daemon", "dagster-webserver"):
        env = _services()[name]["environment"]
        assert {"DAGSTER_HOME", "DAGSTER_PG_HOST", "DAGSTER_PG_PASSWORD", "DAGSTER_CODE_SERVER_HOST"} <= set(env), name
