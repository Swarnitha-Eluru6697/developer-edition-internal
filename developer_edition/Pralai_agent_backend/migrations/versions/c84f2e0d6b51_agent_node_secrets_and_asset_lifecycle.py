"""encrypted agent-node secrets; immutable, retire-able skills and files

Revision ID: c84f2e0d6b51
Revises: b73e1d9c5a40
Create Date: 2026-10-20 19:00:00.000000

* agentnodesecret holds Env values encrypted; flows keep key names only.
  Existing plaintext env values found in agent.flow / agentversion.flow are
  moved into it and scrubbed from both.
* agentskill/agentfile gain sha256 + retired_at (a delete that a published
  version still depends on retires the row instead of removing it); files also
  gain status/status_detail. The (agent_id, name) unique constraint becomes a
  partial unique index over non-retired rows.
"""
import json
import uuid
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c84f2e0d6b51"
down_revision: Union[str, Sequence[str], None] = "b73e1d9c5a40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _scrub(flow, found, version_rank=0):
    """Remove env values from agent nodes; collect {(node_id,key): value}."""
    changed = False
    for n in (flow or {}).get("nodes", []) or []:
        if not isinstance(n, dict) or n.get("type") != "agent" or not isinstance(n.get("config"), dict):
            continue
        env = n["config"].get("env")
        if not isinstance(env, list):
            continue
        rows = []
        for r in env:
            if not isinstance(r, dict):
                continue
            k = str(r.get("key") or "").strip()
            if "value" in r:
                changed = True
                if k and r.get("value"):
                    found.setdefault((n.get("id"), k), r["value"])
            rows.append({"key": k} if k else {"key": ""})
        if rows != env:
            n["config"]["env"] = rows
            changed = True
    return changed


def upgrade() -> None:
    op.add_column("agentskill", sa.Column("sha256", sa.String(), nullable=False, server_default=""))
    op.add_column("agentskill", sa.Column("retired_at", sa.DateTime(), nullable=True))
    op.add_column("agentfile", sa.Column("sha256", sa.String(), nullable=False, server_default=""))
    op.add_column("agentfile", sa.Column("retired_at", sa.DateTime(), nullable=True))
    op.add_column("agentfile", sa.Column("status", sa.String(), nullable=False, server_default="ready"))
    op.add_column("agentfile", sa.Column("status_detail", sa.String(), nullable=False, server_default=""))
    op.drop_constraint("uq_agentskill_agent_name", "agentskill", type_="unique")
    op.create_index("uq_agentskill_agent_name_live", "agentskill", ["agent_id", "name"], unique=True,
                    postgresql_where=sa.text("retired_at IS NULL"))

    op.create_table(
        "agentnodesecret",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value_enc", sa.String(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "node_id", "key", name="uq_agentnodesecret_agent_node_key"),
    )
    op.create_index("ix_agentnodesecret_agent_id", "agentnodesecret", ["agent_id"])

    # Move plaintext env values (v53) into the encrypted table and scrub flows.
    bind = op.get_bind()
    from app.secrets_crypto import encrypt_secret, require_durable_key
    now = datetime.utcnow()
    # Newest published versions first so a value only ever present in history is still recovered.
    ver_rows = bind.execute(sa.text("SELECT id, agent_id, flow FROM agentversion ORDER BY id DESC")).fetchall()
    per_agent = {}
    for vid, aid, flow in ver_rows:
        found = {}
        flow = flow if isinstance(flow, dict) else json.loads(flow or "{}")
        if _scrub(flow, found):
            bind.execute(sa.text("UPDATE agentversion SET flow = CAST(:f AS jsonb) WHERE id = :i"),
                         {"f": json.dumps(flow), "i": vid})
        for k, v in found.items():
            per_agent.setdefault(aid, {}).setdefault(k, v)
    agent_rows = bind.execute(sa.text("SELECT id, flow FROM agent")).fetchall()
    if per_agent or any('"value"' in json.dumps(f if isinstance(f, dict) else {}) for _, f in agent_rows):
        # Moving real values: refuse to encrypt them under a throwaway key.
        require_durable_key("existing Agent-node Env values")
    for aid, flow in agent_rows:
        found = {}
        flow = flow if isinstance(flow, dict) else json.loads(flow or "{}")
        if _scrub(flow, found):
            bind.execute(sa.text("UPDATE agent SET flow = CAST(:f AS jsonb) WHERE id = :i"),
                         {"f": json.dumps(flow), "i": aid})
        merged = {**per_agent.get(aid, {}), **found}  # the draft's value wins
        for (node_id, key), value in merged.items():
            bind.execute(sa.text(
                "INSERT INTO agentnodesecret (id, agent_id, node_id, key, value_enc, updated_at) "
                "VALUES (:id, :a, :n, :k, :v, :t) ON CONFLICT DO NOTHING"),
                {"id": f"ns-{uuid.uuid4().hex[:16]}", "a": aid, "n": node_id, "k": key,
                 "v": encrypt_secret(str(value)), "t": now})


def downgrade() -> None:
    op.drop_index("ix_agentnodesecret_agent_id", table_name="agentnodesecret")
    op.drop_table("agentnodesecret")
    op.drop_index("uq_agentskill_agent_name_live", table_name="agentskill")
    op.create_unique_constraint("uq_agentskill_agent_name", "agentskill", ["agent_id", "name"])
    for c in ("status_detail", "status", "retired_at", "sha256"):
        op.drop_column("agentfile", c)
    op.drop_column("agentskill", "retired_at")
    op.drop_column("agentskill", "sha256")
