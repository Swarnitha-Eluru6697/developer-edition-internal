"""Initial schema — dq_rules, dq_rulesets, dq_ruleset_items, dq_job_bindings, dq_render_cache.

Revision ID: 0001_initial
"""
from __future__ import annotations
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "dq_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("description", sa.Text),
        sa.Column("rule_type", sa.String(40), nullable=False),
        sa.Column("dimension", sa.String(20), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False, server_default="ERROR"),
        sa.Column("parameters", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("tags", postgresql.ARRAY(sa.String), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        sa.Column("is_system_rule", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("rendered_dbt_yaml", sa.Text),
        sa.Column("rendered_flinksql", sa.Text),
        sa.Column("rendered_gx_json", postgresql.JSONB),
        sa.Column("rendered_soda_yaml", sa.Text),
        sa.CheckConstraint(
            "rule_type IN ('not_null','unique','accepted_values','range','regex',"
            "'freshness','row_count','row_count_anomaly','custom_sql','custom_python')",
            name="ck_dq_rule_type"),
        sa.CheckConstraint(
            "dimension IN ('nulls','uniqueness','validity','freshness','volume','schema','custom')",
            name="ck_dq_rule_dimension"),
        sa.CheckConstraint("severity IN ('CRITICAL','ERROR','WARNING','INFO')", name="ck_dq_rule_severity"),
        sa.UniqueConstraint("org_id", "name", name="uq_dq_rule_org_name"),
    )
    op.create_index("ix_dq_rule_org", "dq_rules", ["org_id"])
    op.create_index("ix_dq_rule_dimension", "dq_rules", ["dimension"])

    op.create_table(
        "dq_rulesets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_dq_ruleset_org_name"),
    )
    op.create_index("ix_dq_ruleset_org", "dq_rulesets", ["org_id"])

    op.create_table(
        "dq_ruleset_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("ruleset_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("dq_rulesets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rule_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("dq_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("column_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("parameter_overrides", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("ruleset_id", "rule_id", "column_name", name="uq_dq_item_rule_col"),
    )
    op.create_index("ix_dq_item_ruleset", "dq_ruleset_items", ["ruleset_id"])

    op.create_table(
        "dq_job_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("ruleset_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("dq_rulesets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_type", sa.String(20), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_table", sa.String(500), nullable=False),
        sa.Column("ucs_fqn", sa.String(500), nullable=False),
        sa.Column("enforcement_mode", sa.String(10), nullable=False, server_default="warn"),
        sa.Column("created_by", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("job_type IN ('dbt_project','flink_job','python_job')", name="ck_dq_binding_job_type"),
        sa.CheckConstraint("enforcement_mode IN ('blocking','warn','async')", name="ck_dq_binding_mode"),
        sa.UniqueConstraint("ruleset_id", "job_type", "job_id", "target_table", name="uq_dq_binding_unique"),
    )
    op.create_index("ix_dq_binding_org", "dq_job_bindings", ["org_id"])
    op.create_index("ix_dq_binding_job", "dq_job_bindings", ["job_type", "job_id"])

    op.create_table(
        "dq_render_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("binding_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("dq_job_bindings.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("rules_hash", sa.String(64), nullable=False),
        sa.Column("dbt_artifact", sa.Text),
        sa.Column("flinksql_artifact", sa.Text),
        sa.Column("gx_artifact", postgresql.JSONB),
        sa.Column("soda_artifact", sa.Text),
        sa.Column("rendered_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dq_cache_binding", "dq_render_cache", ["binding_id"])


def downgrade() -> None:
    for tbl in ("dq_render_cache", "dq_job_bindings", "dq_ruleset_items",
                "dq_rulesets", "dq_rules"):
        op.drop_table(tbl)
