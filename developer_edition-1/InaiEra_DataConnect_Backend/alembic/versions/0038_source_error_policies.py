"""source connectors have no dead-letter queue

GAP-04: Kafka Connect provides a dead-letter queue only to SINK connectors. Until now every source pipeline defaulted
to DLQ_AND_CONTINUE (and could pick THRESHOLD), which on a source silently skipped bad records into nothing, and
THRESHOLD counted an empty topic and never tripped. This makes the data say what actually happens:

- source pipelines on DLQ_AND_CONTINUE or THRESHOLD become SKIP_AND_LOG (same errors.tolerance=all behaviour, honest
  name; a THRESHOLD source LOSES nothing it ever had — the threshold never worked — but the threshold settings are cleared);
- source connector templates stop claiming supports_dlq.

Downgrade restores supports_dlq for sources and maps SKIP_AND_LOG sources back to DLQ_AND_CONTINUE.

Revision ID: 0038_source_error_policies
Revises: 0037_quality_sink_constraint
Create Date: 2026-10-01
"""

from alembic import op

revision = "0038_source_error_policies"
down_revision = "0037_quality_sink_constraint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE dc_pipelines
        SET error_handling_policy = 'SKIP_AND_LOG',
            error_threshold_max_errors = NULL,
            error_threshold_window_seconds = NULL
        WHERE upper(direction) = 'SOURCE' AND error_handling_policy IN ('DLQ_AND_CONTINUE', 'THRESHOLD')
        """
    )
    op.execute("UPDATE dc_connector_templates SET supports_dlq = false WHERE connector_type = 'source'")


def downgrade() -> None:
    op.execute("UPDATE dc_connector_templates SET supports_dlq = true WHERE connector_type = 'source'")
    op.execute(
        "UPDATE dc_pipelines SET error_handling_policy = 'DLQ_AND_CONTINUE' "
        "WHERE upper(direction) = 'SOURCE' AND error_handling_policy = 'SKIP_AND_LOG'"
    )
