"""Pipelines record the separator their Kafka topic names are built with ("-" for new pipelines, "." for existing ones)

Topic names used to join their seven parts with "." (org_1001.raw.erp.sap.customers.cdc.v1). Gravitino, the catalog behind
UCS, cannot load an object name with more than two ".", so none of those topics could be browsed there. New pipelines now
use "-" (org_1001-raw-erp-sap-customers-cdc-v1).

Kafka cannot rename a topic and every existing connector is already writing to (or reading from) its dotted one, so the
existing pipelines keep it. What must follow the pipeline is every name DERIVED from it — the prepared topic, the validation
/ connector / worker quarantine topics and the sink dead-letter queue — which is why the separator is stored per pipeline:
those rows get ".", everything created from here on gets "-".

This migration also lets the "a sink may only read its own organization's topics" check accept either separator after the org
id (the org id itself can contain neither, so the check cannot be fooled by another org whose id starts the same way).

Revision ID: 0043_topic_separator
Revises: 0042_connect_worker_pools
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0043_topic_separator"
down_revision = "0042_connect_worker_pools"
branch_labels = None
depends_on = None

_OLD_IN_ORG = "direction <> 'SINK' OR left(connector_config ->> 'topics', length(org_id) + 1) = org_id || '.'"
_NEW_IN_ORG = (
    "direction <> 'SINK' OR left(connector_config ->> 'topics', length(org_id) + 1) IN (org_id || '.', org_id || '-')"
)


def upgrade() -> None:
    # Every row that exists now is a dotted pipeline, so the server default is "."; the application sets "-" explicitly
    # on every pipeline it creates.
    op.add_column("dc_pipelines", sa.Column("topic_separator", sa.String(1), nullable=False, server_default="."))
    op.create_check_constraint("ck_dc_pipelines_topic_separator", "dc_pipelines", "topic_separator IN ('.', '-')")
    op.drop_constraint("ck_dc_pipelines_sink_input_in_org", "dc_pipelines", type_="check")
    op.create_check_constraint("ck_dc_pipelines_sink_input_in_org", "dc_pipelines", _NEW_IN_ORG)


def downgrade() -> None:
    # Dashed sink inputs cannot satisfy the old check: refuse rather than silently drop the pipelines' topics.
    bind = op.get_bind()
    dashed = bind.execute(sa.text("SELECT count(*) FROM dc_pipelines WHERE topic_separator = '-'")).scalar()
    if dashed:
        raise RuntimeError(
            f"{dashed} pipelines use '-'-separated topics; delete them before downgrading, or their topics cannot be read."
        )
    op.drop_constraint("ck_dc_pipelines_sink_input_in_org", "dc_pipelines", type_="check")
    op.create_check_constraint("ck_dc_pipelines_sink_input_in_org", "dc_pipelines", _OLD_IN_ORG)
    op.drop_constraint("ck_dc_pipelines_topic_separator", "dc_pipelines", type_="check")
    op.drop_column("dc_pipelines", "topic_separator")
