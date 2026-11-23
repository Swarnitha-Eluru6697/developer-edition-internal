"""One sink pipeline per connector dead-letter topic, enforced by the database

NEW-08: a sink's connector dead-letter topic is named from domain / source system / entity / version only, so two sinks that differ only in zone or mode
would write ONE topic. The API refuses the second up front (GAP-04), but that check and the insert are separate steps: two concurrent creates could both
pass it and both be stored. A partial UNIQUE index on (org_id, domain, source_system, entity, version) for sinks with a dead-letter policy makes the
database refuse whichever commits second. Sources are excluded (Kafka Connect gives a source no dead-letter queue), as are sinks whose policy keeps no
dead-letter topic.

Refuses to run while two or more sinks ALREADY share a dead-letter topic: a migration must not choose which running connector keeps its queue (changing one
moves its failed records to a different topic). The error lists them. Resolve each group by changing the entity or version of all but one, or by giving all
but one a policy without a dead-letter queue (FAIL_FAST or SKIP_AND_LOG), then run the migration again.

Revision ID: 0053_dead_letter_topic_unique
Revises: 0052_quality_result_job_id
Create Date: 2026-10-13
"""

import sqlalchemy as sa

from alembic import op

revision = "0053_dead_letter_topic_unique"
down_revision = "0052_quality_result_job_id"
branch_labels = None
depends_on = None

INDEX = "uq_dc_pipelines_dead_letter_topic"
# The policies that keep a dead-letter topic (app/core/error_policies.DLQ_POLICIES, written out: a migration records history and must not change when
# the constant does).
_IS_DLQ_SINK = "direction = 'SINK' AND error_handling_policy IN ('DLQ_AND_CONTINUE', 'THRESHOLD')"

_SHARED = f"""
SELECT org_id, domain, source_system, entity, version, string_agg(name || ' (' || id::text || ')', ', ' ORDER BY name) AS pipelines
FROM dc_pipelines
WHERE {_IS_DLQ_SINK}
GROUP BY org_id, domain, source_system, entity, version
HAVING count(*) > 1
ORDER BY org_id, domain, source_system, entity, version
"""


def upgrade() -> None:
    shared = op.get_bind().execute(sa.text(_SHARED)).fetchall()
    if shared:
        listing = "\n".join(
            f"  - org {org}: {domain}/{source}/{entity}/{version} is used by {pipelines}" for org, domain, source, entity, version, pipelines in shared
        )
        raise RuntimeError(
            f"{len(shared)} dead-letter queue(s) are shared by more than one sink pipeline, which is no longer allowed:\n{listing}\n"
            "Change the entity or version of all but one pipeline in each group, or give all but one a policy without a dead-letter "
            "queue (FAIL_FAST or SKIP_AND_LOG), then run this migration again."
        )
    op.create_index(
        INDEX, "dc_pipelines", ["org_id", "domain", "source_system", "entity", "version"], unique=True, postgresql_where=sa.text(_IS_DLQ_SINK),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="dc_pipelines")
