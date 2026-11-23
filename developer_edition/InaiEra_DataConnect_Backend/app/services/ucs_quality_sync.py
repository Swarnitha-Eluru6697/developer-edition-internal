"""
Sync DataConnect's quality-check results to the Unified Catalog Service (UCS)
as an OpenLineage RunEvent carrying a `dataQualityAssertions` facet, POSTed to
the same `POST /api/v1/lineage/events` endpoint app/services/ucs_lineage_sync.py
already uses for lineage. UCS resolves the event's output dataset
(namespace, name) to a Gravitino FQN via its own admin-managed namespace
mapping and stores the assertion against that FQN — the same resolution path
that module's own docstring describes, and for the same reason this module
never computes a Gravitino FQN itself.

Two callers:
  * emit_quality_result — QualityService.execute_policy, right after a QualityResult row is persisted (the create-time sample check and the
    manual Execute / execute-jobs batch path).
  * emit_stream_quality — the continuous QualityStreamWorker (app/services/quality_stream_worker.py), which produces no QualityResult of its
    own: it reports the pipeline's running totals (dc_quality_stream_stats) whenever they changed, at most once per
    settings.ucs_quality_stream_sync_interval_seconds. Without it a pipeline checked only by the worker never had a quality signal in UCS.

One assertion per result, named after the quality POLICY rather than
each individual rule: DataConnect's own summary already aggregates every
rule in a policy into one pass/fail outcome per batch
(build_quality_result_summary's rule_name="*") — UCS's DqCheck model is
keyed on (fqn, name), so using the policy's own name as that stable name
means every subsequent run of the same policy against the same topic updates
the SAME check's current score and score-history trend in UCS, rather than
minting a new one-off check on every run.

The event goes to /lineage/events, so it carries ucs:lineage-events:create — the permission UCS requires on that endpoint
(UCS docs/auth/UCS-PERMISSIONS.md). ucs:quality-results:create belongs to UCS's own /quality/results endpoints, which this module does not
call; sending it here made UCS answer 403 to every quality result.

Opt-in (settings.ucs_quality_sync_enabled — independent of
ucs_lineage_sync_enabled, though it shares that setting's base_url/user) and
best-effort/non-blocking, reusing ucs_lineage_sync.py's own
ol_dataset/send_ol_event helpers and header pattern: a UCS outage must never
fail a quality check.
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.services.ucs_lineage_sync import LINEAGE_EVENTS_PERMISSION, ol_dataset, send_ol_event


def _assertion_event(
    *, org_id: str, policy: dict[str, Any], topic_name: str, total: int, failed: int, run_id: str, result_status: str | None = None,
    status: str | None = None, check_name: str | None = None,
) -> dict[str, Any]:
    assertion_name = check_name or policy.get("name") or f"policy-{policy['id']}"
    passed = max(total - failed, 0)
    # This is a volumetric check (N of M records), not a single true/false
    # assertion — UCS's dq_run.pass_rate lets its score reflect the real
    # percentage instead of collapsing to a flat 0 for any batch with a
    # failure. A batch where every record failed is a genuine "fail"; one
    # where only some did is a "warn", not a "fail" — UCS's score for a
    # failing dimension is hard-capped regardless of the computed percentage
    # (quality_service.py::recompute_score's DQ_FAIL_SCORE_CAP), so reporting
    # "fail" here for a partial failure would silently throw the real
    # pass-rate number away.
    pass_rate = round(100 * passed / total, 4) if total else None
    if status is None:
        if not total:
            status = "pass" if result_status == "passed" else "fail"
        elif failed == 0:
            status = "pass"
        elif passed == 0:
            status = "fail"
        else:
            status = "warn"
    output = {
        **ol_dataset("kafka_topic", topic_name),
        "facets": {
            "dataQualityAssertions": {
                "assertions": [
                    {
                        "assertion": assertion_name,
                        "success": failed == 0,
                        "status": status,
                        "passRate": pass_rate,
                        "dimension": "validity",
                        "detail": f"{failed}/{total} failed",
                    }
                ]
            }
        },
    }
    return {
        "eventType": "COMPLETE",
        "job": {"namespace": "dataconnect", "name": f"quality:{assertion_name}"},
        "run": {"runId": run_id},
        "inputs": [],
        "outputs": [output],
    }


async def emit_quality_result(org_id: str, policy: dict[str, Any], result: dict[str, Any]) -> None:
    if not settings.ucs_quality_sync_enabled:
        return
    topic_name = result.get("topic_name")
    if not topic_name:
        return
    event = _assertion_event(
        org_id=org_id, policy=policy, topic_name=topic_name, total=result.get("total_count") or 0,
        failed=result.get("failed_count") or 0, run_id=f"quality-{result['id']}", result_status=result.get("status"),
    )
    await send_ol_event(org_id, event, permission=LINEAGE_EVENTS_PERMISSION)


async def emit_stream_quality(org_id: str, pipeline: dict[str, Any], policy: dict[str, Any], *, topic_name: str, total: int,
                              failed: int, status: str | None = None, run_suffix: str = "", check_name: str | None = None) -> None:
    """The stream worker's running totals for one pipeline, as one assertion on one topic: the topic it checks, and the topics it writes
    (see QualityStreamWorker._report_to_ucs). The run id names the totals, so a repeated report of the same numbers is the same run in UCS;
    `run_suffix` keeps the topics of one report apart. `status` overrides the computed one (a quarantine topic: 0%, but "warn").
    `check_name` names the UCS check instead of the policy (a sink's check of a topic another pipeline produces — see the worker)."""
    if not settings.ucs_quality_sync_enabled or not total:
        return
    event = _assertion_event(
        org_id=org_id, policy=policy, topic_name=topic_name, total=total, failed=failed,
        run_id=f"quality-stream-{pipeline['id']}-{total}-{failed}{run_suffix}", status=status, check_name=check_name,
    )
    await send_ol_event(org_id, event, permission=LINEAGE_EVENTS_PERMISSION)
