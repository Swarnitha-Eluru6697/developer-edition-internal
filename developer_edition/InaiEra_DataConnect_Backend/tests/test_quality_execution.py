from app.services.quality import build_quality_result_summary, evaluate_record


def test_quality_record_evaluation_supports_required_regex_and_allowed_values() -> None:
    policy = {
        "rules": [
            {"name": "id_required", "type": "required", "field": "id"},
            {"name": "email_format", "type": "regex", "field": "email", "pattern": ".+@.+"},
            {
                "name": "status_allowed",
                "type": "allowed_values",
                "field": "status",
                "values": ["ACTIVE", "INACTIVE"],
            },
        ]
    }

    result = evaluate_record({"id": "", "email": "bad", "status": "PENDING"}, policy)

    assert result["valid"] is False
    assert result["failed_rules"] == ["id_required", "email_format", "status_allowed"]


def test_quality_summary_builds_quarantine_samples() -> None:
    policy = {"id": "dqp", "rules": [{"name": "id_required", "type": "required", "field": "id"}]}

    summary, quarantine_envelopes = build_quality_result_summary(
        org_id="org_1001",
        pipeline_id="pipe_1",
        quality_policy_id="dqp",
        topic_name="org_1001.raw.crm.salesforce.accounts.api.v1",
        records=[{"id": "1"}, {"id": ""}],
        policy=policy,
    )

    assert summary["status"] == "failed"
    assert summary["failed_count"] == 1
    assert summary["sample_failures"][0]["failure_type"] == "quality_rule_failed"
    assert quarantine_envelopes[0]["failure_type"] == "quality_rule_failed"


def test_quality_summary_bounds_sample_failures_but_keeps_full_envelope_list(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "quality_max_sample_failures", 2)
    policy = {"id": "dqp", "rules": [{"name": "id_required", "type": "required", "field": "id"}]}
    records = [{"id": ""} for _ in range(5)]

    summary, quarantine_envelopes = build_quality_result_summary(
        org_id="org_1001",
        pipeline_id="pipe_1",
        quality_policy_id="dqp",
        topic_name="org_1001.raw.crm.salesforce.accounts.api.v1",
        records=records,
        policy=policy,
    )

    assert summary["failed_count"] == 5
    assert len(summary["sample_failures"]) == 2
    assert len(quarantine_envelopes) == 5


def test_quality_summary_explicit_sample_limit_overrides_the_setting_default(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "quality_max_sample_failures", 1)
    policy = {"id": "dqp", "rules": [{"name": "id_required", "type": "required", "field": "id"}]}
    records = [{"id": ""} for _ in range(5)]

    summary, _ = build_quality_result_summary(
        org_id="org_1001",
        pipeline_id="pipe_1",
        quality_policy_id="dqp",
        topic_name="org_1001.raw.crm.salesforce.accounts.api.v1",
        records=records,
        policy=policy,
        sample_limit=len(records),
    )

    assert len(summary["sample_failures"]) == 5

