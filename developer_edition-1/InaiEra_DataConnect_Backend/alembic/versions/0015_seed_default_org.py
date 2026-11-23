"""seed default org

Revision ID: 0015_seed_default_org
Revises: 0014_pipeline_history_fk_ondelete
Create Date: 2026-09-19

"""

from alembic import op

revision = '0015_seed_default_org'
down_revision = '0014_pipeline_history_fk_ondelete'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Insert the default org_1001 organization
    op.execute(
        """
        INSERT INTO dc_organizations (org_id, name, status)
        VALUES ('org_1001', 'Default Organization', 'active')
        ON CONFLICT (org_id) DO NOTHING;
        """
    )


def downgrade() -> None:
    # Remove the default org_1001 organization
    op.execute("DELETE FROM dc_organizations WHERE org_id = 'org_1001'")
