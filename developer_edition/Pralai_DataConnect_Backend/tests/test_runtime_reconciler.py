from app.services.runtime_reconciler import (
    derive_observed_state,
    is_converged,
    next_generation,
    plan_actions,
)


def test_derive_observed_state_absent_when_connector_missing() -> None:
    assert derive_observed_state(False, None) == "absent"


def test_derive_observed_state_unknown_when_status_missing() -> None:
    assert derive_observed_state(True, None) == "unknown"


def test_derive_observed_state_running() -> None:
    status = {"connector": {"state": "RUNNING"}, "tasks": [{"state": "RUNNING"}]}
    assert derive_observed_state(True, status) == "running"


def test_derive_observed_state_paused() -> None:
    status = {"connector": {"state": "PAUSED"}, "tasks": [{"state": "PAUSED"}]}
    assert derive_observed_state(True, status) == "paused"


def test_derive_observed_state_failed_connector() -> None:
    status = {"connector": {"state": "FAILED"}, "tasks": []}
    assert derive_observed_state(True, status) == "failed"


def test_derive_observed_state_degraded_when_connector_running_but_task_failed() -> None:
    """The connector itself is up but a task is down — distinct from a fully
    failed connector: some data may still be flowing on surviving tasks."""
    status = {"connector": {"state": "RUNNING"}, "tasks": [{"state": "FAILED"}]}
    assert derive_observed_state(True, status) == "degraded"


def test_derive_observed_state_failed_connector_overrides_task_state() -> None:
    status = {"connector": {"state": "FAILED"}, "tasks": [{"state": "RUNNING"}]}
    assert derive_observed_state(True, status) == "failed"


def test_is_converged_deleted_requires_absent() -> None:
    assert is_converged("deleted", "absent") is True
    assert is_converged("deleted", "unknown") is False


def test_is_converged_running_family_requires_running() -> None:
    for desired in ("deployed", "running"):
        assert is_converged(desired, "running") is True
        assert is_converged(desired, "paused") is False
        # Strict on purpose: a degraded or outright failed connector is a
        # real observation, but it is not convergence — self-healing keeps
        # retrying every tick until it's genuinely running.
        assert is_converged(desired, "degraded") is False
        assert is_converged(desired, "failed") is False


def test_is_converged_paused_requires_paused() -> None:
    assert is_converged("paused", "paused") is True
    assert is_converged("paused", "running") is False


def test_next_generation_bumps_only_on_change() -> None:
    assert next_generation(3, state_changed=False, config_changed=False) == 3
    assert next_generation(3, state_changed=True, config_changed=False) == 4
    assert next_generation(3, state_changed=False, config_changed=True) == 4


def test_plan_actions_deletes_only_if_connector_exists() -> None:
    assert plan_actions("deleted", connector_exists=True, config_changed=False, observed_state="running") == {
        "delete": True
    }
    assert plan_actions("deleted", connector_exists=False, config_changed=False, observed_state="absent") == {
        "delete": False
    }


def test_plan_actions_deploys_before_any_state_change() -> None:
    """A crash-recovered or manually-deleted connector must be redeployed first —
    state actions (pause/resume/restart) wait for a later tick once it exists."""
    actions = plan_actions("running", connector_exists=False, config_changed=False, observed_state="absent")
    assert actions == {"deploy": True}

    actions = plan_actions("running", connector_exists=True, config_changed=True, observed_state="running")
    assert actions == {"deploy": True}


def test_plan_actions_pauses_and_resumes() -> None:
    actions = plan_actions("paused", connector_exists=True, config_changed=False, observed_state="running")
    assert actions == {"deploy": False, "pause": True}

    actions = plan_actions("running", connector_exists=True, config_changed=False, observed_state="paused")
    assert actions == {"deploy": False, "resume": True}


def test_plan_actions_self_heals_failed_connector() -> None:
    """Kafka Connect failed the connector on its own (worker restart, manual
    edit, transient error) — the reconciler restarts it without being asked."""
    actions = plan_actions("running", connector_exists=True, config_changed=False, observed_state="failed")
    assert actions == {"deploy": False, "restart": True}


def test_plan_actions_self_heals_degraded_connector() -> None:
    """A connector with one failed task also gets restarted, not just a
    fully failed connector."""
    actions = plan_actions("running", connector_exists=True, config_changed=False, observed_state="degraded")
    assert actions == {"deploy": False, "restart": True}


def test_plan_actions_has_no_persistent_restarting_desired_state() -> None:
    """A restart is a one-shot action issued by pipeline_service, not a
    persistent desired_state — plan_actions must never special-case it
    (that bug would re-issue a restart on every tick forever)."""
    actions = plan_actions("restarting", connector_exists=True, config_changed=False, observed_state="running")
    assert actions == {"deploy": False}


def test_plan_actions_no_op_when_already_converged() -> None:
    actions = plan_actions("running", connector_exists=True, config_changed=False, observed_state="running")
    assert actions == {"deploy": False}
