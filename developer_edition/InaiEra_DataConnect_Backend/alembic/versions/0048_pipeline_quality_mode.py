"""A pipeline's quality policy is applied in a stated mode: OBSERVE or ENFORCE

NEW-01: the quality worker used to only OBSERVE a pipeline's topic (evaluate, count, copy failures to quarantine) while every consumer of that
topic still received the failing records. `quality_mode` makes the choice explicit. Every existing pipeline becomes OBSERVE, which is exactly
what it already did, so nothing changes until someone chooses ENFORCE. ENFORCE routes passing records to the prepared topic and failures to
quarantine; it is only valid with a quality policy (also enforced by the database).

Revision ID: 0048_pipeline_quality_mode
Revises: 0047_quality_job_resubmitted_from
Create Date: 2026-10-13
"""

import sqlalchemy as sa

from alembic import op

revision = "0048_pipeline_quality_mode"
down_revision = "0047_quality_job_resubmitted_from"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dc_pipelines",
        sa.Column("quality_mode", sa.String(10), nullable=False, server_default="OBSERVE",
                  comment="How the quality policy is applied: OBSERVE (count and copy failures to quarantine; the data topic is unchanged) | "
                          "ENFORCE (passing records to the prepared topic, failures to quarantine)"),
    )
    op.create_check_constraint("ck_dc_pipelines_quality_mode", "dc_pipelines", "quality_mode IN ('OBSERVE', 'ENFORCE')")
    op.create_check_constraint(
        "ck_dc_pipelines_enforce_needs_policy", "dc_pipelines", "quality_mode <> 'ENFORCE' OR quality_policy_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_constraint("ck_dc_pipelines_enforce_needs_policy", "dc_pipelines", type_="check")
    op.drop_constraint("ck_dc_pipelines_quality_mode", "dc_pipelines", type_="check")
    op.drop_column("dc_pipelines", "quality_mode")
