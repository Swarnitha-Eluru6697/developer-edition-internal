"""a pipeline's direction decides its shape: sinks have no generated topic, and the database enforces it

GAP-12. Until now every pipeline carried a generated `topic_name`, including sinks — which produce nothing to Kafka and
read topics that already exist. That invented topic was returned by the API, attributed by monitoring and drawn into
lineage as if the sink wrote to it. This migration:

  1. makes `topic_name` nullable and clears it on every SINK pipeline (a source keeps its generated topic);
  2. removes the lineage that was materialized for sinks as if they were sources (their edges and any asset left
     orphaned by that) — the application re-materializes a sink's lineage in its own shape on the next deploy;
  3. adds CHECK constraints so the rows can no longer be inconsistent:
       - direction is SOURCE or SINK;
       - a SINK has no topic_name, a SOURCE has one;
       - a SINK names an input topic, and its first input topic belongs to its organization;
       - a SOURCE carries no sink-only `topics` setting.

Before adding the constraints it checks every existing row and refuses to continue — listing the offenders — rather
than silently altering data it was not asked to change (clearing a sink's generated topic is the one deliberate edit).

Revision ID: 0040_pipeline_direction_integrity
Revises: 0039_discovery_bounds
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0040_pipeline_direction_integrity"
down_revision = "0039_discovery_bounds"
branch_labels = None
depends_on = None

_CHECKS = {
    "ck_dc_pipelines_direction": "direction IN ('SOURCE', 'SINK')",
    "ck_dc_pipelines_topic_by_direction": (
        "(direction = 'SINK' AND topic_name IS NULL) OR (direction = 'SOURCE' AND topic_name IS NOT NULL)"
    ),
    "ck_dc_pipelines_sink_input_topic": (
        "direction <> 'SINK' OR coalesce(connector_config ->> 'topics', '') <> ''"
    ),
    "ck_dc_pipelines_sink_input_in_org": (
        "direction <> 'SINK' OR left(connector_config ->> 'topics', length(org_id) + 1) = org_id || '.'"
    ),
    "ck_dc_pipelines_source_has_no_topics_setting": (
        "direction <> 'SOURCE' OR NOT jsonb_exists(connector_config, 'topics')"
    ),
}

# What each check would reject, to list offenders by name before any constraint is added. (The topic_name check is
# evaluated AFTER sinks are cleared, so it only reports sources without a topic and rows with a bad direction.)
_OFFENDER_QUERIES = {
    "ck_dc_pipelines_direction": "SELECT name FROM dc_pipelines WHERE NOT (direction IN ('SOURCE', 'SINK'))",
    "ck_dc_pipelines_topic_by_direction": (
        "SELECT name FROM dc_pipelines WHERE NOT ((direction = 'SINK' AND topic_name IS NULL) "
        "OR (direction = 'SOURCE' AND topic_name IS NOT NULL))"
    ),
    "ck_dc_pipelines_sink_input_topic": (
        "SELECT name FROM dc_pipelines WHERE direction = 'SINK' AND coalesce(connector_config ->> 'topics', '') = ''"
    ),
    "ck_dc_pipelines_sink_input_in_org": (
        "SELECT name FROM dc_pipelines WHERE direction = 'SINK' "
        "AND left(connector_config ->> 'topics', length(org_id) + 1) IS DISTINCT FROM org_id || '.'"
    ),
    "ck_dc_pipelines_source_has_no_topics_setting": (
        "SELECT name FROM dc_pipelines WHERE direction = 'SOURCE' AND jsonb_exists(connector_config, 'topics')"
    ),
}


def upgrade() -> None:
    bind = op.get_bind()
    op.alter_column("dc_pipelines", "topic_name", existing_type=sa.String(500), nullable=True)

    # (A colon followed by a word inside op.execute SQL reads as a bind parameter, hence the split ':' || 'quality'.)
    # 2. lineage that treated a sink like a source — before its topic_name is cleared, since the topic assets are found by it.
    op.execute(
        """
        CREATE TEMP TABLE _gap12_sinks ON COMMIT DROP AS
        SELECT id, org_id, domain, source_system, entity, connector_name, topic_name
        FROM dc_pipelines WHERE direction = 'SINK'
        """
    )
    op.execute("DELETE FROM dc_lineage_edges WHERE pipeline_id IN (SELECT id FROM _gap12_sinks)")
    op.execute(
        """
        DELETE FROM dc_assets a USING _gap12_sinks s
        WHERE a.org_id = s.org_id
          AND a.qualified_name IN (
              s.org_id || ':source:' || s.domain || ':' || s.source_system || ':' || s.entity,
              s.org_id || ':connector:' || s.connector_name,
              s.org_id || ':processor:' || s.id::text || ':' || 'quality',
              s.org_id || ':topic:' || s.topic_name,
              s.org_id || ':topic:' || replace(s.topic_name, '.raw.', '.prepared.')
          )
          AND NOT EXISTS (SELECT 1 FROM dc_lineage_edges e WHERE e.from_asset_id = a.id OR e.to_asset_id = a.id)
        """
    )

    # 1. a sink generates no topic.
    op.execute("UPDATE dc_pipelines SET topic_name = NULL WHERE direction = 'SINK'")

    # 3. refuse to add a constraint over rows that already break it.
    problems = []
    for name, query in _OFFENDER_QUERIES.items():
        offenders = [row[0] for row in bind.execute(sa.text(query)).fetchall()]
        if offenders:
            problems.append(f"{name}: {', '.join(sorted(offenders))}")
    if problems:
        raise RuntimeError(
            "Cannot add the pipeline direction constraints — these pipelines violate them and need fixing first:\n  "
            + "\n  ".join(problems)
        )
    for name, expression in _CHECKS.items():
        op.create_check_constraint(name, "dc_pipelines", expression)


def downgrade() -> None:
    for name in _CHECKS:
        op.drop_constraint(name, "dc_pipelines", type_="check")
    # A sink's generated topic can be recomputed (topic_naming.build_topic_name) but not from SQL alone; restore the
    # NOT NULL contract with a placeholder derived from the sink's own identity so the column can be tightened again.
    op.execute(
        "UPDATE dc_pipelines SET topic_name = org_id || '.' || zone || '.' || domain || '.' || source_system || '.' "
        "|| entity || '.' || mode || '.' || version WHERE topic_name IS NULL"
    )
    op.alter_column("dc_pipelines", "topic_name", existing_type=sa.String(500), nullable=False)
