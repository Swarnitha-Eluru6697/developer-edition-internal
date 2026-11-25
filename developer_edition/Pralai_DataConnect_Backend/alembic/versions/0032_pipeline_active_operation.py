"""pipeline active_operation_id

FE-BUG-25: idempotent lifecycle-action submission. deploy()/pause()/
resume()/restart() previously only persisted their transitional status
(deploying/pausing/resuming/restarting) at the END of a live Kafka Connect
round-trip — for the whole duration of that call, and for however long a
queued Operation sat waiting for a worker slot, the pipeline row still
showed its OLD status, so two near-simultaneous submissions (a double-click,
or two browser tabs) could both read that old status, both pass
_assert_transition, and both proceed.

active_operation_id is set by pipeline_service.claim_lifecycle_transition —
a single atomic conditional UPDATE run at SUBMISSION time, before any
Operation row is created — so the pipeline's status flips to "deploying"
(etc.) immediately, and a second submission's own claim attempt finds the
row no longer in an eligible source status and is rejected before it ever
creates a duplicate Operation. It also gives the frontend an operation id to
resume polling after a page refresh, per this ticket's own "refreshing the
page should rediscover the active operation" requirement.

Deliberately not a foreign key to dc_operations — same rationale as
ConnectorTemplateRevision.template_id (see 0004's model docstring): a
pointer by id, not a hard constraint, so operation/pipeline lifecycle
management never has to worry about FK ordering on either side.

Revision ID: 0032_pipeline_active_operation
Revises: 0031_connector_template_capabilities
Create Date: 2026-09-24
"""

import sqlalchemy as sa

from alembic import op

revision = "0032_pipeline_active_operation"
down_revision = "0031_connector_template_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dc_pipelines", sa.Column("active_operation_id", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("dc_pipelines", "active_operation_id")
