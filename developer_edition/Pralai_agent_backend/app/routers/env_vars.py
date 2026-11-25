"""
Per-agent environment variables — set once, referenced from any node's
config via {{env.KEY}} (see harness.py's interpolate_env, which does the
real substitution at execution time, node types that have any string
config field).

Security note on Secret-type variables: masking here isn't just a CSS trick
on an already-fetched plaintext value — the raw value is genuinely never
included in any response after the initial create/update call. Every GET
returns a fixed mask for secret vars; only harness.py, running server-side
during execution, ever reads the real value from the database.
"""
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel

from ..db import get_session
from ..models import Agent, EnvVar, AgentLog
from ..secrets_crypto import encrypt_secret

router = APIRouter(prefix="/api/agents", tags=["env-vars"])

MASK = "••••••••"
VALID_TYPES = {"string", "number", "secret"}


class EnvVarIn(BaseModel):
    key: str
    value: str
    type: str = "string"
    description: str = ""


class EnvVarUpdate(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None


def _masked(v: EnvVar) -> dict:
    """Never include a secret's real value in an API response."""
    return {
        "id": v.id, "agent_id": v.agent_id, "key": v.key, "type": v.type,
        "description": v.description,
        "value": MASK if v.type == "secret" else v.value,
        "created_at": v.created_at,
    }


def _validate(body_type: str, key: str):
    if body_type not in VALID_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(VALID_TYPES)}")
    if not key or not key.replace("_", "").isalnum():
        raise HTTPException(400, "key must be alphanumeric/underscore, e.g. API_BASE_URL")


def _audit_secret(session: Session, agent_id: str, action: str, key: str):
    """Real audit logging (§0.59, GAP_REPORT) around real secret
    lifecycle events — reuses this platform's own existing AgentLog
    mechanism (already used for moderation/blocked events elsewhere)
    rather than inventing a separate, parallel logging system. Never
    logs the real value itself, only that a real change happened, to
    which real key, when — the value staying out of the log is the
    whole point."""
    session.add(AgentLog(
        agent_id=agent_id, ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3],
        level="INFO", msg=f'Secret env var "{key}" was {action}',
    ))


@router.get("/{agent_id}/env", dependencies=[Depends(require_permission(P.STUDIO))])
def list_env_vars(agent_id: str, session: Session = Depends(get_session)) -> List[dict]:
    if not session.get(Agent, agent_id):
        raise HTTPException(404, "Agent not found")
    rows = session.exec(select(EnvVar).where(EnvVar.agent_id == agent_id)).all()
    return [_masked(r) for r in rows]


@router.post("/{agent_id}/env", dependencies=[Depends(require_permission(P.STUDIO))])
def create_env_var(agent_id: str, body: EnvVarIn, session: Session = Depends(get_session)) -> dict:
    """§0.70, GAP_REPORT — the real (agent_id, key) uniqueness check
    below is now genuinely enforced by a real Postgres unique
    constraint (see EnvVar's own model docstring), not just this
    SELECT. The SELECT-based check stays as a fast, honest common-case
    rejection (most real duplicate attempts aren't a genuine race, just
    a person or a caller trying the same name twice), but the actual,
    race-proof guarantee is the real IntegrityError caught below — two
    genuinely concurrent creates of the same variable can no longer
    both succeed; the loser gets the same real "already exists" error
    the winner's own later GET would show them, not a raw 500."""
    if not session.get(Agent, agent_id):
        raise HTTPException(404, "Agent not found")
    _validate(body.type, body.key)
    existing = session.exec(
        select(EnvVar).where(EnvVar.agent_id == agent_id, EnvVar.key == body.key)
    ).first()
    if existing:
        raise HTTPException(400, f"A variable named {body.key} already exists for this agent")

    # Real encryption at rest for secret-type values (§0.59, GAP_REPORT)
    # — the row's own stored value is no longer the real plaintext.
    stored_value = encrypt_secret(body.value) if body.type == "secret" else body.value
    row = EnvVar(agent_id=agent_id, key=body.key, value=stored_value, type=body.type, description=body.description)
    session.add(row)
    if body.type == "secret":
        _audit_secret(session, agent_id, "created", body.key)
    try:
        session.commit()
    except IntegrityError:
        # The real race this fix closes: a genuinely concurrent create
        # of the same (agent_id, key) committed first. Postgres's own
        # real unique constraint caught it — roll back this session's
        # own failed attempt and report the same honest outcome the
        # SELECT-based check above already gives for the common,
        # non-racing case.
        session.rollback()
        raise HTTPException(400, f"A variable named {body.key} already exists for this agent")
    session.refresh(row)
    # the ONE moment the real value is echoed back — the immediate response
    # to the create call the user just made, same principle as e.g. a cloud
    # provider showing an API key once at generation time. Every later GET
    # of this same row goes through _masked() and never shows it again.
    # Uses body.value (the real plaintext the caller sent), not row.value
    # (now the real encrypted form) — this was already correct before
    # encryption existed and stays correct without any change here.
    result = _masked(row)
    if body.type == "secret":
        result["value"] = body.value
    return result


@router.patch("/{agent_id}/env/{var_id}", dependencies=[Depends(require_permission(P.STUDIO))])
def update_env_var(agent_id: str, var_id: int, body: EnvVarUpdate, session: Session = Depends(get_session)) -> dict:
    row = session.get(EnvVar, var_id)
    if not row or row.agent_id != agent_id:
        raise HTTPException(404, "Variable not found")
    if body.value is not None:
        row.value = encrypt_secret(body.value) if row.type == "secret" else body.value
        if row.type == "secret":
            _audit_secret(session, agent_id, "rotated", row.key)
    if body.description is not None:
        row.description = body.description
    session.add(row)
    session.commit()
    session.refresh(row)
    result = _masked(row)
    if row.type == "secret" and body.value is not None:
        result["value"] = body.value  # same one-time-echo rule as create
    return result


@router.delete("/{agent_id}/env/{var_id}", dependencies=[Depends(require_permission(P.STUDIO))])
def delete_env_var(agent_id: str, var_id: int, session: Session = Depends(get_session)):
    row = session.get(EnvVar, var_id)
    if not row or row.agent_id != agent_id:
        raise HTTPException(404, "Variable not found")
    if row.type == "secret":
        _audit_secret(session, agent_id, "deleted", row.key)
    session.delete(row)
    session.commit()
    return {"deleted": var_id}
