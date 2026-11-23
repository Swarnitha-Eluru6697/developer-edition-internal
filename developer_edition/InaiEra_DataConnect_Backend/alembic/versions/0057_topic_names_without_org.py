"""Topic and connector names no longer carry the organization

2026-11-06: a topic is named `<zone>-<domain>-<source_system>-<entity>-<mode>-<version>` and a connector `<domain>--<source_system>--<entity>--<mode>--<version>`;
the organization id is no longer part of either. An organization owns its own Kafka, so its names need no prefix to be told apart. Three database rules
leaned on the old names and change here:

1. `ck_dc_pipelines_sink_input_in_org` required a sink's input topic to START with the org id. The topic has no org part any more, so every sink would be
   refused. Dropped.
2. The connector dead-letter queue index was unique per (org_id, domain, source_system, entity, version). The dead-letter topic has no org part either, so
   two organizations that name a sink alike would write ONE topic: the index is now unique across organizations (domain, source_system, entity, version).
3. The topic and the connector name are now unique across organizations (`uq_dc_pipelines_topic_name`, `uq_dc_pipelines_connector_name`). Per organization they
   already were (a unique constraint on (org_id, topic_name) and (org_id, connector_name)), because the org id made every name distinct; without it two
   organizations on one Kafka / Connect cluster could otherwise create the same topic or connector.

Refuses to run while two organizations ALREADY hold a name that would collide (listed in the error); a migration must not choose whose pipeline keeps it.
Topics and connectors that exist keep the names stored on their rows (`org_1001-raw-...`), which the platform still reads; only new pipelines get the new names.

Revision ID: 0057_topic_names_without_org
Revises: 0056_pipeline_record_format
Create Date: 2026-11-06
"""

import sqlalchemy as sa

from alembic import op

revision = "0057_topic_names_without_org"
down_revision = "0056_pipeline_record_format"
branch_labels = None
depends_on = None

OLD_IN_ORG = "ck_dc_pipelines_sink_input_in_org"
_OLD_IN_ORG_SQL = "direction <> 'SINK' OR left(connector_config ->> 'topics', length(org_id) + 1) = org_id || '-'"
DEAD_LETTER_INDEX = "uq_dc_pipelines_dead_letter_topic"
# The policies that keep a dead-letter topic (app/core/error_policies.DLQ_POLICIES, written out: a migration records history and must not change when
# the constant does).
_IS_DLQ_SINK = "direction = 'SINK' AND error_handling_policy IN ('DLQ_AND_CONTINUE', 'THRESHOLD')"
TOPIC_INDEX = "uq_dc_pipelines_topic_name"
CONNECTOR_INDEX = "uq_dc_pipelines_connector_name"

_SHARED_DEAD_LETTER = f"""
SELECT domain, source_system, entity, version, string_agg(org_id || ': ' || name, ', ' ORDER BY org_id, name) AS pipelines
FROM dc_pipelines WHERE {_IS_DLQ_SINK}
GROUP BY domain, source_system, entity, version HAVING count(*) > 1
ORDER BY domain, source_system, entity, version
"""
_SHARED_TOPIC = """
SELECT topic_name AS name, string_agg(org_id || ': ' || name, ', ' ORDER BY org_id, name) AS pipelines
FROM dc_pipelines WHERE topic_name IS NOT NULL GROUP BY topic_name HAVING count(*) > 1 ORDER BY topic_name
"""
_SHARED_CONNECTOR = """
SELECT connector_name AS name, string_agg(org_id || ': ' || name, ', ' ORDER BY org_id, name) AS pipelines
FROM dc_pipelines GROUP BY connector_name HAVING count(*) > 1 ORDER BY connector_name
"""


def _refuse_if_any(rows, what: str) -> None:
    if rows:
        listing = "\n".join(f"  - {' / '.join(str(v) for v in row[:-1])}: {row[-1]}" for row in rows)
        raise RuntimeError(
            f"{len(rows)} {what} are held by more than one organization, which is no longer allowed now that names carry no organization:\n{listing}\n"
            "Rename (change the entity or version of) all but one pipeline in each group, then run this migration again."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _refuse_if_any(bind.execute(sa.text(_SHARED_DEAD_LETTER)).fetchall(), "sink dead-letter queue name(s)")
    _refuse_if_any(bind.execute(sa.text(_SHARED_TOPIC)).fetchall(), "topic name(s)")
    _refuse_if_any(bind.execute(sa.text(_SHARED_CONNECTOR)).fetchall(), "connector name(s)")

    op.drop_constraint(OLD_IN_ORG, "dc_pipelines", type_="check")
    op.drop_index(DEAD_LETTER_INDEX, table_name="dc_pipelines")
    op.create_index(
        DEAD_LETTER_INDEX, "dc_pipelines", ["domain", "source_system", "entity", "version"], unique=True, postgresql_where=sa.text(_IS_DLQ_SINK),
    )
    op.create_index(TOPIC_INDEX, "dc_pipelines", ["topic_name"], unique=True, postgresql_where=sa.text("topic_name IS NOT NULL"))
    op.create_index(CONNECTOR_INDEX, "dc_pipelines", ["connector_name"], unique=True)


def downgrade() -> None:
    op.drop_index(CONNECTOR_INDEX, table_name="dc_pipelines")
    op.drop_index(TOPIC_INDEX, table_name="dc_pipelines")
    op.drop_index(DEAD_LETTER_INDEX, table_name="dc_pipelines")
    op.create_index(
        DEAD_LETTER_INDEX, "dc_pipelines", ["org_id", "domain", "source_system", "entity", "version"], unique=True, postgresql_where=sa.text(_IS_DLQ_SINK),
    )
    # NOT VALID: rows created after the upgrade read org-less topics, which the old rule would refuse; it still guards every new or changed row.
    op.execute(f"ALTER TABLE dc_pipelines ADD CONSTRAINT {OLD_IN_ORG} CHECK ({_OLD_IN_ORG_SQL}) NOT VALID")
