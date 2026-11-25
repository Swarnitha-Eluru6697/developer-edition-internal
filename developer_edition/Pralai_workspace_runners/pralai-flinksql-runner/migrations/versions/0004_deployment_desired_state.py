"""Add flink_deployments.parallelism/stop_savepoint_dir/stop_drain —
persisted desired state for a resumed deploy/stop attempt after a
restart (RUN-FLINK-013), previously only local variables in the API
request that started the (now non-durable) background task.

Revision ID: 0004_deployment_desired_state
Revises:     0003_quality_result_outbox
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0004_deployment_desired_state"
down_revision: Union[str, None] = "0003_quality_result_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("flink_deployments",
                  sa.Column("parallelism", sa.Integer, nullable=False, server_default="1"))
    op.add_column("flink_deployments", sa.Column("stop_savepoint_dir", sa.String(1000)))
    op.add_column("flink_deployments",
                  sa.Column("stop_drain", sa.Boolean, nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("flink_deployments", "stop_drain")
    op.drop_column("flink_deployments", "stop_savepoint_dir")
    op.drop_column("flink_deployments", "parallelism")
