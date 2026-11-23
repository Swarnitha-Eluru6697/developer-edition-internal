"""DG-001: the production chart states who owns cron scheduling, and cannot silently hand it to nobody.

Dagster's Helm chart deploys one generic code location whose job has no sensors, so in a chart-based
deployment nothing outside the runner turns a project's cron into runs. The chart therefore keeps the
runner's own scheduler (RUN-DBT-004) ON by default, and refuses to switch it off unless the operator
names the external orchestrator that really does replace it.

Needs `helm` on PATH (skipped otherwise); no app imports, so it runs without the service's dependencies.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "docker" / "helm" / "dbtrunner"
pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")


def _template(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "r", str(CHART), "--set", "tenant.orgId=acme", "-f", str(CHART / "ci" / "egress-values.yaml"), *extra],
                          capture_output=True, text=True, check=False)


def _configmap(done: subprocess.CompletedProcess) -> dict:
    assert done.returncode == 0, done.stderr
    (cm,) = [d for d in yaml.safe_load_all(done.stdout) if d and d.get("kind") == "ConfigMap"]
    return cm["data"]


def test_native_scheduler_is_explicitly_on_by_default():
    assert _configmap(_template())["SCHEDULER_ENABLED"] == "true"


def test_turning_the_native_scheduler_off_requires_naming_the_replacement():
    done = _template("--set", "scheduler.enabled=false")
    assert done.returncode != 0, "chart rendered with NO scheduler owner"
    assert "scheduler.externalOrchestrator" in done.stderr


def test_turning_it_off_with_a_named_orchestrator_is_allowed():
    data = _configmap(_template("--set", "scheduler.enabled=false", "--set", "scheduler.externalOrchestrator=dagster"))
    assert data["SCHEDULER_ENABLED"] == "false"


def test_a_named_orchestrator_does_not_disable_the_scheduler_by_itself():
    data = _configmap(_template("--set", "scheduler.externalOrchestrator=dagster"))
    assert data["SCHEDULER_ENABLED"] == "true"
