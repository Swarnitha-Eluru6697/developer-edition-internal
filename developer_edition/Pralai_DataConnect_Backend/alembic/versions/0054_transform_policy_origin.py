"""Transform policies record how they were made: by hand ("custom") or by the pipeline form ("auto")

The Transforms page has a Type column — Custom for an SMT created with "+ New SMT", Auto for one a pipeline created while it was being built. The pipeline
form creates its policy through the same endpoint as the button, so nothing in the row said which was which except a description anyone can edit. The
frontend now sends origin="auto" for the pipeline's policy; this adds the column (default "custom") and labels the ones that already exist: a policy whose
description is the text the pipeline form has always written is "auto", everything else "custom".

Revision ID: 0054_transform_policy_origin
Revises: 0053_dead_letter_topic_unique
Create Date: 2026-10-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0054_transform_policy_origin"
down_revision = "0053_dead_letter_topic_unique"
branch_labels = None
depends_on = None

# The description the pipeline form writes (written out: a migration records history and must not change with the frontend).
_AUTO_DESCRIPTION = "Auto-created from selected source transformations"


def upgrade() -> None:
    op.add_column("dc_transform_policies", sa.Column("origin", sa.String(length=20), nullable=False, server_default="custom"))
    op.create_check_constraint("ck_dc_transform_policies_origin", "dc_transform_policies", "origin IN ('custom', 'auto')")
    op.execute(
        sa.text("UPDATE dc_transform_policies SET origin = 'auto' WHERE description = :d").bindparams(d=_AUTO_DESCRIPTION)
    )


def downgrade() -> None:
    op.drop_constraint("ck_dc_transform_policies_origin", "dc_transform_policies", type_="check")
    op.drop_column("dc_transform_policies", "origin")
