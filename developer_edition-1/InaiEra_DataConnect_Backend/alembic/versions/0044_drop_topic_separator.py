"""Topic names are joined with "-" only: drop the per-pipeline separator column and the "." allowance on sink inputs

Migration 0043 added `dc_pipelines.topic_separator` so a pipeline created before the switch could keep its dotted topic
("." — org_1001.raw.erp.sap.customers.cdc.v1) while new ones got "-". The "." join has since been removed from the
application: a topic is built, parsed and derived from the seven fields with "-" only (app/services/topic_naming.py), nothing
reads the column and it always holds "-". This drops it, along with its CHECK constraint, and tightens the sink-input
constraint so a sink's input topic must start with "<org_id>-" (it used to also accept "<org_id>.").

Refuses to run while any pipeline still has a dotted topic or a dotted sink input: those would be orphaned by this change, and
a migration must not silently strand a running connector's topic. Move or delete them first.

Revision ID: 0044_drop_topic_separator
Revises: 0043_topic_separator
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0044_drop_topic_separator"
down_revision = "0043_topic_separator"
branch_labels = None
depends_on = None

_DASHED_ONLY = "direction <> 'SINK' OR left(connector_config ->> 'topics', length(org_id) + 1) = org_id || '-'"
_DOT_OR_DASH = (
    "direction <> 'SINK' OR left(connector_config ->> 'topics', length(org_id) + 1) IN (org_id || '.', org_id || '-')"
)

_DOTTED_PIPELINES = """
SELECT count(*) FROM dc_pipelines
WHERE topic_separator = '.'
   OR topic_name LIKE '%.%'
   OR (direction = 'SINK' AND left(connector_config ->> 'topics', length(org_id) + 1) = org_id || '.')
"""


def upgrade() -> None:
    dotted = op.get_bind().execute(sa.text(_DOTTED_PIPELINES)).scalar()
    if dotted:
        raise RuntimeError(
            f"{dotted} pipelines still use '.'-separated topics. The '.' join is no longer supported: delete or recreate them "
            "(their connectors and topics would otherwise be orphaned), then run this migration again."
        )
    op.drop_constraint("ck_dc_pipelines_sink_input_in_org", "dc_pipelines", type_="check")
    op.create_check_constraint("ck_dc_pipelines_sink_input_in_org", "dc_pipelines", _DASHED_ONLY)
    op.drop_constraint("ck_dc_pipelines_topic_separator", "dc_pipelines", type_="check")
    op.drop_column("dc_pipelines", "topic_separator")


def downgrade() -> None:
    # Every pipeline is "-" by now, so that is what the restored column holds (0043 used "." as the server default for rows that
    # already existed then; none can exist any more).
    op.add_column("dc_pipelines", sa.Column("topic_separator", sa.String(1), nullable=False, server_default="-"))
    op.create_check_constraint("ck_dc_pipelines_topic_separator", "dc_pipelines", "topic_separator IN ('.', '-')")
    op.drop_constraint("ck_dc_pipelines_sink_input_in_org", "dc_pipelines", type_="check")
    op.create_check_constraint("ck_dc_pipelines_sink_input_in_org", "dc_pipelines", _DOT_OR_DASH)
