"""DB-backed setup helpers for binding/apply tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.models import JobBinding, Rule, Ruleset, RulesetItem

ORG = "test-org"


async def seed(db, *, job_type="dbt_project", mode="warn", rules=None, resolved=True,
               target_table="orders", fail_stop=False, ucs_fqn="pg.public.orders",
               resolution=None):
    """rules: list of (rule_type, column, params, extra) -> returns ids."""
    rules = rules or [("not_null", "order_id", {}, {})]
    async with db() as s:
        rs = Ruleset(org_id=ORG, name=f"rs-{uuid.uuid4().hex[:6]}", version=1)
        s.add(rs)
        await s.flush()
        rule_rows = []
        for i, (rt, col, params, extra) in enumerate(rules):
            r = Rule(org_id=ORG, name=f"{rt}-{i}-{uuid.uuid4().hex[:4]}", rule_type=rt,
                     dimension=extra.get("dimension", "validity"),
                     severity=extra.get("severity", "ERROR"), parameters=params, tags=[],
                     version=1, privileged=rt == "custom_sql",
                     review_status=extra.get("review_status",
                                             "approved" if rt == "custom_sql" else None))
            s.add(r)
            await s.flush()
            s.add(RulesetItem(ruleset_id=rs.id, rule_id=r.id, column_name=col,
                              parameter_overrides={}, enabled=True, sort_order=i))
            rule_rows.append(r)
        b = JobBinding(org_id=ORG, ruleset_id=rs.id, job_type=job_type, job_id=uuid.uuid4(),
                       target_table=target_table, ucs_fqn=ucs_fqn, enforcement_mode=mode,
                       flink_fail_stop=fail_stop, version=1,
                       resolved_at=datetime.now(timezone.utc) if resolved else None,
                       resolution=resolution or {"job": {"verified": True, "engine": "trino"},
                                                 "dataset": {"verified": True}})
        s.add(b)
        await s.commit()
        return {"ruleset_id": rs.id, "binding_id": b.id, "job_id": b.job_id,
                "rule_ids": [r.id for r in rule_rows]}
