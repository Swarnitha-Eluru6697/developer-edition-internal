"""Versioned rules/rulesets/bindings, binding resolution, immutable
content-addressed artifacts, apply records, UCS outbox, service-token jti.

SAFE TO APPLY TO A LIVE DATABASE AT 0002 (real rules/rulesets/bindings):
  * every new column is NULLable or has a server default; nothing is
    rewritten except these backfills:
      - custom_sql rules -> privileged=true, review_status='pending'
        (they must be re-approved by a dq.custom_sql.write holder before
        they render again — DQ-SEC-006);
      - existing dq_artifact_versions rows get kind='legacy' (server
        default) and hash_version NULL: they were produced under the old,
        weaker hash and are never reused or applied, but are kept as history.
  * the unique (binding_id, content_hash) index is PARTIAL
    (hash_version IS NOT NULL), so pre-existing duplicate legacy rows (old
    forced re-renders inserted the same hash twice) cannot make it fail.
  * the only columns dropped are dq_rules.rendered_* — a regenerable render
    cache that the application only ever set to NULL.
  * the UPDATE-blocking trigger on dq_artifact_versions is created AFTER the
    backfills above.

Revision ID: 0003_dq_review_fixes
Revises: 0002_artifact_versions
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_dq_review_fixes"
down_revision: Union[str, None] = "0002_artifact_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    # ── dq_rules ─────────────────────────────────────────────────────────
    op.add_column("dq_rules", sa.Column("version", sa.Integer, nullable=False,
                                        server_default=sa.text("1")))
    op.add_column("dq_rules", sa.Column("privileged", sa.Boolean, nullable=False,
                                        server_default=sa.false()))
    op.add_column("dq_rules", sa.Column("review_status", sa.String(20)))
    op.add_column("dq_rules", sa.Column("reviewed_by", sa.String(255)))
    op.add_column("dq_rules", sa.Column("reviewed_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE dq_rules SET privileged = true, review_status = 'pending' "
               "WHERE rule_type = 'custom_sql'")
    op.create_check_constraint(
        "ck_dq_rule_review_status", "dq_rules",
        "review_status IS NULL OR review_status IN ('pending','approved')")
    for col in ("rendered_dbt_yaml", "rendered_flinksql", "rendered_gx_json", "rendered_soda_yaml"):
        op.execute(f"ALTER TABLE dq_rules DROP COLUMN IF EXISTS {col}")

    # ── dq_rulesets ──────────────────────────────────────────────────────
    op.add_column("dq_rulesets", sa.Column("version", sa.Integer, nullable=False,
                                           server_default=sa.text("1")))

    # ── dq_job_bindings ──────────────────────────────────────────────────
    op.add_column("dq_job_bindings", sa.Column("flink_fail_stop", sa.Boolean, nullable=False,
                                               server_default=sa.false()))
    op.add_column("dq_job_bindings", sa.Column("version", sa.Integer, nullable=False,
                                               server_default=sa.text("1")))
    op.add_column("dq_job_bindings", sa.Column("job_name", sa.String(255)))
    op.add_column("dq_job_bindings", sa.Column("job_kind", sa.String(40)))
    op.add_column("dq_job_bindings", sa.Column("job_org_id", sa.String(255)))
    op.add_column("dq_job_bindings", sa.Column("dataset_fqn", sa.String(500)))
    op.add_column("dq_job_bindings", sa.Column("dataset_object_type", sa.String(40)))
    op.add_column("dq_job_bindings", sa.Column("resolution", postgresql.JSONB))
    op.add_column("dq_job_bindings", sa.Column("resolved_at", sa.DateTime(timezone=True)))

    # ── dq_artifact_versions ─────────────────────────────────────────────
    op.add_column("dq_artifact_versions", sa.Column("kind", sa.String(20), nullable=False,
                                                    server_default=sa.text("'legacy'")))
    for name, type_ in (
        ("hash_version", sa.Integer), ("hash_inputs", postgresql.JSONB),
        ("ruleset_id", _UUID), ("ruleset_version", sa.Integer),
        ("binding_version", sa.Integer), ("rule_versions", postgresql.JSONB),
        ("target_model", sa.String(500)), ("source_plan_hash", sa.String(64)),
        ("normalized_sql_hash", sa.String(64)), ("contract_version", sa.String(20)),
        ("overrides", postgresql.JSONB), ("supported_engine_versions", postgresql.JSONB),
        ("capability", postgresql.JSONB), ("validation_status", sa.String(20)),
        ("created_by", sa.String(255)),
    ):
        op.add_column("dq_artifact_versions", sa.Column(name, type_))
    op.create_index("uq_dq_artifact_binding_hash_v2", "dq_artifact_versions",
                    ["binding_id", "content_hash"], unique=True,
                    postgresql_where=sa.text("hash_version IS NOT NULL"))
    op.execute("""
        CREATE OR REPLACE FUNCTION dq_artifact_versions_immutable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'dq_artifact_versions rows are immutable (DQ-ART-004)';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_dq_artifact_versions_immutable
        BEFORE UPDATE ON dq_artifact_versions
        FOR EACH ROW EXECUTE FUNCTION dq_artifact_versions_immutable()
    """)

    # ── new tables ───────────────────────────────────────────────────────
    op.create_table(
        "dq_artifact_applications",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("binding_id", _UUID, sa.ForeignKey("dq_job_bindings.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("artifact_version_id", _UUID,
                  sa.ForeignKey("dq_artifact_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("detail", postgresql.JSONB),
        sa.Column("actor", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_dq_application_idempotency_key"),
    )
    op.create_index("ix_dq_application_binding", "dq_artifact_applications", ["binding_id"])

    op.create_table(
        "dq_outbox",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("idempotency_key", name="uq_dq_outbox_idempotency_key"),
        sa.CheckConstraint("status IN ('pending','delivered','failed')",
                           name="ck_dq_outbox_status"),
    )
    op.create_index("ix_dq_outbox_status_next", "dq_outbox", ["status", "next_attempt_at"])

    op.create_table(
        "dq_service_token_jti",
        sa.Column("jti", sa.String(128), primary_key=True),
        sa.Column("issuer", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("dq_service_token_jti")
    op.drop_index("ix_dq_outbox_status_next", table_name="dq_outbox")
    op.drop_table("dq_outbox")
    op.drop_index("ix_dq_application_binding", table_name="dq_artifact_applications")
    op.drop_table("dq_artifact_applications")

    op.execute("DROP TRIGGER IF EXISTS trg_dq_artifact_versions_immutable ON dq_artifact_versions")
    op.execute("DROP FUNCTION IF EXISTS dq_artifact_versions_immutable()")
    op.drop_index("uq_dq_artifact_binding_hash_v2", table_name="dq_artifact_versions")
    for name in ("created_by", "validation_status", "capability", "supported_engine_versions",
                 "overrides", "contract_version", "normalized_sql_hash", "source_plan_hash",
                 "target_model", "rule_versions", "binding_version", "ruleset_version",
                 "ruleset_id", "hash_inputs", "hash_version", "kind"):
        op.drop_column("dq_artifact_versions", name)

    for name in ("resolved_at", "resolution", "dataset_object_type", "dataset_fqn", "job_org_id",
                 "job_kind", "job_name", "version", "flink_fail_stop"):
        op.drop_column("dq_job_bindings", name)
    op.drop_column("dq_rulesets", "version")

    op.add_column("dq_rules", sa.Column("rendered_dbt_yaml", sa.Text))
    op.add_column("dq_rules", sa.Column("rendered_flinksql", sa.Text))
    op.add_column("dq_rules", sa.Column("rendered_gx_json", postgresql.JSONB))
    op.add_column("dq_rules", sa.Column("rendered_soda_yaml", sa.Text))
    op.drop_constraint("ck_dq_rule_review_status", "dq_rules", type_="check")
    for name in ("reviewed_at", "reviewed_by", "review_status", "privileged", "version"):
        op.drop_column("dq_rules", name)
