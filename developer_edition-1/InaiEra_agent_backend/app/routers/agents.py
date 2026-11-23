import copy
from datetime import datetime
from uuid import uuid4
from typing import Optional, List, Dict
from fastapi import APIRouter, Depends, HTTPException, Header, Request, Response
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission

from .. import agent_guard, ucs_publish
from sqlmodel import Session, select
from sqlalchemy import update as sql_update, delete as sql_delete
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, ConfigDict

from ..db import get_session
from .. import ucs_client
from .. import agent_catalog

import logging

logger = logging.getLogger(__name__)
from ..models import (Agent, AgentLog, Role, Trace, Template, TemplateVersion, AgentVersion, WebhookEndpoint,
                      SavedResult, Run, EvalSet, EvalCase, EvalRun, EvalResult, Alert,
                      HitlRequest, ChatMessage, EnvVar, AgentMemory)
from ..pagination import paginate, pagination_params
from ..flow_validator import SUPPORTED_NODE_TYPES, CONTAINER_TYPES, valid_source_handles, validate_flow
from ..flow_compiler import objects_to_check, governed_objects
from ..limits import check_flow_size
from ..auth import get_verified_identity

router = APIRouter(prefix="/api/agents", tags=["agents"])


class NewAgentIn(BaseModel):
    # Stable template ids are the only supported template selector. Unknown
    # fields are rejected so an obsolete `template_name` request cannot
    # silently create an unrelated blank Agent.
    model_config = ConfigDict(extra="forbid")

    name: str
    subtype: str
    owner: str = "you"
    app_type: Optional[str] = None
    template_id: Optional[str] = None  # stable published template identity
    template_flow: Optional[dict] = None  # direct flow injection for generated/internal creation paths
    plane: str = "App"  # real, explicit "App" | "Snippet" (§0.38, GAP_REPORT) — "Data" agents are provisioned elsewhere, unaffected


class NodeIn(BaseModel):
    type: str
    x: float
    y: float
    sub: Optional[str] = ""
    config: dict = {}
    parent_id: Optional[str] = None  # set when this node is nested inside a loop container


class NodeMoveIn(BaseModel):
    x: float
    y: float


class NodeConfigIn(BaseModel):
    config: dict


# Runs that reached an outcome. queued/running/paused are still in flight and say
# nothing yet about whether the agent works.
_FINISHED = ("success", "failed", "blocked", "denied", "cancelled")


def _live_stats(session: Session, agent_ids: list) -> dict:
    """Runs, cost and health over the last 7 days, computed from the Run table.

    Agent.runs_7d / cost_7d / health_* are stored columns that nothing keeps current: the
    seeded agents carry their demo values for ever, and every agent created since sits at
    0 / $0.00 / "not run" however much it runs — the Overview showed "not run" for agents
    with 200 runs this week. These values are computed per request and returned in place
    of the stored ones; the columns themselves are left alone.

    Health is the success rate over finished runs: pass >= 90%, warn >= 70%, fail below.
    An agent with no finished runs in the window keeps "—" ("not run"), which is now true."""
    if not agent_ids:
        return {}
    from datetime import timedelta
    from sqlalchemy import func
    since = datetime.utcnow() - timedelta(days=7)
    rows = session.exec(
        select(
            Run.agent_id,
            func.count(),
            func.coalesce(func.sum(Run.cost_usd), 0),
            func.count().filter(Run.status == "success"),
            func.count().filter(Run.status.in_(_FINISHED)),
        ).where(Run.agent_id.in_(agent_ids), Run.created_at.is_not(None), Run.created_at >= since)
        .group_by(Run.agent_id)
    ).all()
    out = {}
    for agent_id, runs, cost, ok, finished in rows:
        score = round(100 * ok / finished) if finished else None
        status = ("—" if score is None else "pass" if score >= 90 else "warn" if score >= 70 else "fail")
        out[agent_id] = {"runs_7d": int(runs), "cost_7d": f"${float(cost):,.2f}",
                         "health_score": score, "health_status": status}
    return out


def _with_live_stats(session: Session, agents: list) -> list:
    stats = _live_stats(session, [a.id for a in agents])
    idle = {"runs_7d": 0, "cost_7d": "$0.00", "health_score": None, "health_status": "—"}
    return [{**a.model_dump(), **stats.get(a.id, idle)} for a in agents]


@router.get("", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def list_agents(
    response: Response, plane: Optional[str] = None, session: Session = Depends(get_session),
    page: dict = Depends(pagination_params),
) -> List[dict]:
    """Real `plane` filter (§0.38, GAP_REPORT) — Studio's own grid asks
    for plane != 'Snippet' implicitly by requesting 'Data'/'App'
    individually where it already did, and the new Snippets page asks
    for plane='Snippet' specifically, so the two real lists never mix.

    §0.113, GAP_REPORT — the same real, keyset-pagination fix as
    runs.py/traces.py's own identical gap; see pagination.py's own
    module docstring for the full real reasoning. A real, honest,
    stated difference from those: Agent has no real created_at column
    of its own (unlike Run/Trace) — sorted by its own real, unique
    `id` alone, the same real pattern AgentLog's own fix already
    established for a collection with no separate, meaningful sort
    column. This is a real, stable, correct pagination order; it is
    NOT a chronological one (Agent.id is a real, opaque random string,
    not a monotonic value) — a real, deliberate, stated trade-off
    against adding a new schema column for cosmetic ordering alone."""
    stmt = select(Agent)
    if plane:
        stmt = stmt.where(Agent.plane == plane)
    rows = paginate(session, Agent, stmt, limit=page["limit"], cursor=page["cursor"],
                    sort_col=Agent.id, id_col=Agent.id, response=response)
    return _with_live_stats(session, rows)


@router.get("/published-for-user", dependencies=[Depends(require_permission(P.WEBAPPS))])
def list_published_for_user(invoking_user: str, session: Session = Depends(get_session)) -> List[Agent]:
    """Real per-agent UCS check, not a local ACL table — the Web Apps
    page calls this to build its sidebar. For every
    published App agent, asks UCS's real /api/v1/authz/check whether
    this invoking_user may invoke it. Grant/revoke lives entirely in
    UCS's own console (§ ucs_client.py's own docstring); this endpoint
    never makes that decision itself, only relays it.

    Matches this platform's existing "fails loud" governance stance: if
    UCS is unreachable, this raises (503) rather than silently showing
    every agent or none — a governance gate that fails open on an outage
    is worse than one that fails loud."""
    from ..ucs_client import check_agent_access, UCSUnavailable
    published = session.exec(select(Agent).where(Agent.status == "published", Agent.type == "App")).all()
    accessible = []
    for a in published:
        try:
            decision = check_agent_access(invoking_user, a.id, a.name)
        except UCSUnavailable as e:
            raise HTTPException(503, f"Could not resolve app access — UCS unreachable: {e}")
        if decision["allowed"]:
            accessible.append(a)
    return accessible


@router.get("/node-schema/unwired-controls", dependencies=[Depends(require_permission(P.STUDIO))])
def get_unwired_node_controls() -> Dict[str, List[str]]:
    """§0.115, GAP_REPORT — the actual, real, machine-readable "runtime
    schema" layer the report's own recommended architecture calls for:
    `Node JSON schema -> runtime schema -> frontend generated from the
    same schema`. Returns the real, single source of truth
    (node_wiring.py's own UNWIRED_CONTROLS) the Builder's own config
    panels now query directly, instead of each one hand-maintaining
    its own, separately-drifting warning text about which controls a
    node's real handler doesn't actually consume yet."""
    from ..node_wiring import UNWIRED_CONTROLS
    return UNWIRED_CONTROLS


@router.get("/{agent_id}", dependencies=[Depends(require_any_permission(*P.AGENT_READERS, P.WEBAPPS))])
def get_agent(agent_id: str, session: Session = Depends(get_session)) -> dict:
    a = session.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    return _with_live_stats(session, [a])[0]


# --- lineage and quality -------------------------------------------------
#
# Computed here, from this platform's own tables, and published to UCS.
# This is the thing that runs agents, so it is the only thing that knows
# what an agent read and how well it did. UCS is where those facts are
# published so an agent sits alongside every other governed object — it
# is not where they are looked up.


@router.get("/{agent_id}/lineage", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def get_agent_lineage(agent_id: str, session: Session = Depends(get_session)) -> dict:
    """What this agent reads, from its own flow and run history.

    Replaces a hardcoded panel that showed every agent reading
    `lakehouse.sales.orders_daily` and writing a "Generated report".
    """
    agent = _get_or_404(agent_id, session)
    return agent_catalog.agent_lineage(session, agent)


@router.get("/{agent_id}/quality", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def get_agent_quality(agent_id: str, session: Session = Depends(get_session)) -> dict:
    """This agent's measured quality, from its own evals and runs.

    Replaces a panel that reported a hardcoded 92% eval pass rate and
    "none" drift for every agent.
    """
    agent = _get_or_404(agent_id, session)
    return agent_catalog.agent_quality(session, agent)


@router.get("/{agent_id}/live-jobs", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def get_agent_live_jobs(agent_id: str, session: Session = Depends(get_session)) -> dict:
    """Runs of this agent that have not finished.

    This tab was a fixed "No live jobs running right now" — it said that
    whatever was actually happening, and there were five running runs on
    this install while it did.
    """
    _get_or_404(agent_id, session)
    rows = session.exec(
        select(Run).where(Run.agent_id == agent_id,
                          Run.status.in_(("running", "queued", "paused")))
        .order_by(Run.created_at.desc())).all()
    now = datetime.utcnow()
    return {"jobs": [{
        "id": r.id,
        "status": r.status,
        "started_at": r.created_at.isoformat() if r.created_at else None,
        # How long it has been going, which is the thing you actually
        # want to know about something that has not finished.
        "running_for_s": int((now - r.created_at).total_seconds()) if r.created_at else None,
        "invoking_user": r.invoking_user,
        "workflow_id": r.workflow_id,
    } for r in rows]}


@router.post("/{agent_id}/catalog/publish", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def publish_agent_catalog(agent_id: str, session: Session = Depends(get_session)) -> dict:
    """Push this agent's measured quality to UCS now.

    Lineage publishes itself on every run; quality is a measurement over
    a window rather than an event, so it is pushed on demand and by the
    scheduled sweep.
    """
    agent = _get_or_404(agent_id, session)
    result = agent_catalog.publish_quality(session, agent)
    # Attributes go with it: the API-access, traces, logs and live-jobs
    # rollups are facts about the object, and publishing quality without
    # them would leave the catalogue half updated.
    result["attributes"] = ucs_publish.publish_attributes(agent, session=session)
    return result


@router.delete("/{agent_id}", dependencies=[Depends(require_permission(P.STUDIO))])
def delete_agent(agent_id: str, request: Request,
                 session: Session = Depends(get_session)) -> dict:
    """Deletes a DRAFT agent and everything that hangs off it.

    Draft-only, enforced here and not just in the Studio UI that hides
    the button. A staged or published agent is genuinely different: it
    has a live WebhookEndpoint, a published surface real end users can
    open at #/app/{id}, an AgentVersion other rows point at, and (once
    a UCS gateway secret is configured) a registered Gravitino Model in UCS
    that deleting rows here would NOT retract — leaving a catalogue
    entry pointing at nothing. Refusing is the honest behaviour; use
    unpublish first if that is ever wanted.

    The deletes are ordered children-before-parents because these are
    real foreign keys, not ON DELETE CASCADE ones: HitlRequest points
    at Run, EvalResult at EvalRun, EvalCase at EvalSet, and Run/Trace/
    EvalRun/WebhookEndpoint all point at AgentVersion. Postgres would
    reject the parent delete otherwise. Nodes and edges need no step of
    their own — `flow` is a JSONB column on the Agent row itself.
    """
    # Agent RBAC: UCS decides, this line is where the service asks.
    agent_guard.require(request, agent_id, "delete")
    a = _get_or_404(agent_id, session)
    if a.status != "draft":
        raise HTTPException(
            409,
            f"Only draft agents can be deleted; '{a.name}' is {a.status}. "
            "Unpublish it first if it genuinely should be removed.",
        )

    # The eval chain is EvalSet -> EvalCase and EvalSet -> EvalRun ->
    # EvalResult, so it unwinds deepest-first. An EvalRun is matched on
    # BOTH its own agent_id and its parent set: agent_id carries no
    # foreign key of its own, so matching only that could leave a stray
    # run behind and the EvalSet delete would then fail on a real FK.
    eval_set_ids = select(EvalSet.id).where(EvalSet.agent_id == agent_id)
    eval_runs = (EvalRun.agent_id == agent_id) | (EvalRun.eval_set_id.in_(eval_set_ids))
    eval_run_ids = select(EvalRun.id).where(eval_runs)
    session.exec(sql_delete(EvalResult).where(EvalResult.eval_run_id.in_(eval_run_ids)))
    session.exec(sql_delete(EvalRun).where(eval_runs))
    session.exec(sql_delete(EvalCase).where(EvalCase.eval_set_id.in_(eval_set_ids)))
    session.exec(sql_delete(EvalSet).where(EvalSet.agent_id == agent_id))

    # Then every remaining direct child. HitlRequest precedes Run
    # because it points at run.id, and AgentVersion is last because
    # Run, Trace and WebhookEndpoint all point at agentversion.id.
    for model in (HitlRequest, Trace, Run, WebhookEndpoint,
                  AgentLog, SavedResult, Alert, Role, ChatMessage, EnvVar,
                  AgentVersion):
        session.exec(sql_delete(model).where(model.agent_id == agent_id))

    # AgentMemory has no FK — it keys on scope_key, which is either the
    # bare agent id ('agent' scope) or "{agent_id}:{invoking_user}"
    # ('conversation' scope), so both shapes have to go.
    session.exec(sql_delete(AgentMemory).where(
        (AgentMemory.scope_key == agent_id) | (AgentMemory.scope_key.startswith(f"{agent_id}:"))
    ))

    # Skills, reference files and node Env secrets hold real foreign keys to
    # the agent; their object-store blobs are removed once the rows are gone.
    from ..agent_assets import delete_agent_assets, delete_blobs
    blob_keys = delete_agent_assets(session, agent_id)

    session.delete(a)
    session.commit()
    delete_blobs(blob_keys)
    return {"deleted": agent_id}


@router.post("", dependencies=[Depends(require_permission(P.STUDIO))])
def create_agent(body: NewAgentIn, session: Session = Depends(get_session)) -> Agent:
    """Real template usage — previously `template_flow` existed on this
    model but the frontend never actually sent it, so every agent got the
    same 3-node skeleton regardless of which template was picked. Passing
    `template_id` looks the published template up server-side and deep-copies its
    real flow (so the new agent's flow is independent of the template's —
    editing one never mutates the other).

    Always creates a real App agent — Data agents are provisioned by a
    separate service, not built fresh through this endpoint. The Data
    agents already seeded in this system are unaffected; this only scopes
    what a person can newly create here."""
    new_id = f"a{uuid4().hex[:10]}"
    flow = None
    tmpl = None
    tmpl_version = None
    if body.template_id:
        tmpl = session.get(Template, body.template_id)
        if not tmpl or tmpl.hidden or tmpl.status != "published" or not tmpl.current_version_id:
            raise HTTPException(404, f"Published template '{body.template_id}' not found")
        tmpl_version = session.get(TemplateVersion, tmpl.current_version_id)
        if not tmpl_version:
            raise HTTPException(409, "Template publication metadata is inconsistent: published version is missing")
        flow = copy.deepcopy(tmpl_version.flow)
    # A real snippet starts as a genuinely empty canvas — matching
    # Dify's own documented behavior exactly: "no Start node (User
    # Input or Triggers) and no end node." The standard trigger/gov/
    # respond scaffold below is real and correct for a normal App
    # agent, but would be a real, structural mismatch for a snippet.
    # A new agent starts with the two nodes every flow has in common: the
    # Trigger, and the UCS check wired straight after it. Everything else
    # is the person's to drag in.
    #
    # It used to arrive as trigger -> gov -> respond, then briefly as the
    # trigger alone. The respond node was a guess about a flow nobody had
    # designed yet and is a palette node now. The UCS check is different:
    # it belongs to every flow, so it is placed rather than added — and it
    # carries its own on/off switch (config["enabled"]) so it can be
    # stood down without being deleted and without leaving a gap in the
    # graph.
    #
    # Switching it off does not switch entitlement checking off:
    # _execute_flow runs the same check as pre-flight whenever no enabled
    # gov node will do it. See the gate there and handle_gov's docstring.
    default_flow = (
        {"nodes": [], "edges": []} if body.plane == "Snippet" else
        {
            "nodes": [
                {"id": "n1", "type": "trigger", "x": 40, "y": 140, "sub": "user input", "config": {}},
                {"id": "n2", "type": "gov", "x": 220, "y": 140, "sub": "entitlement gate",
                 "config": {"enabled": True}},
            ],
            "edges": [
                {"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
            ],
        }
    )
    flow = flow or body.template_flow or default_flow
    # template_flow is the one path that accepts a WHOLE flow in a single
    # request — every other write goes node-by-node through _save_flow,
    # where the same cap already applies. Without this, agent creation
    # was the way around it.
    check_flow_size((flow or {}).get("nodes"), (flow or {}).get("edges"))
    agent = Agent(
        id=new_id, name=body.name, type="App", subtype=body.subtype,
        plane=body.plane, owner=body.owner, status="draft", version="v1",
        health_status="—", flow=flow, metrics={},
        app_type=(tmpl_version.app_type if tmpl_version and tmpl_version.app_type else body.app_type),
        # Stable provenance points at the exact immutable template version.
        template_name=(tmpl_version.name if tmpl_version else None),
        template_id=(tmpl.id if tmpl else None),
        template_version_id=(tmpl_version.id if tmpl_version else None),
    )
    session.add(agent)
    session.commit()
    # A template's embedded skills and reference-file text become real library
    # assets of the new agent (fresh ids); files the template only *requires*
    # stay listed on the node for the person to upload.
    from ..agent_assets import materialize_embedded_assets, delete_blobs, AssetError
    new_flow = copy.deepcopy(agent.flow)
    written: list = []
    try:
        written = materialize_embedded_assets(session, agent.id, new_flow)
        if new_flow != agent.flow:
            agent.flow = new_flow
            session.add(agent)
        session.commit()
    except Exception as exc:
        session.rollback()
        # materialize removes its own blobs when it fails part-way; this covers a
        # failure after it returned (the commit). No object is left without a row.
        delete_blobs(written)
        row = session.get(Agent, agent.id)
        if row is not None:
            session.delete(row)
            session.commit()
        if isinstance(exc, AssetError):
            raise HTTPException(exc.status, exc.detail())
        raise
    # Bind the new agent's service principal to the deployment's default
    # group. The agent-principal half of the publish entitlement check
    # reads groups from the local `role` table, and only seed.py ever
    # wrote those — so every agent created through the product had none
    # and that half could never pass, no matter how the catalog was
    # granted. No-op unless UCS_AGENT_DEFAULT_GROUP (or
    # UCS_AUTO_GRANT_GROUP) names a group.
    from ..ucs_provisioning import bind_agent_to_default_group
    bind_agent_to_default_group(session, agent.id, agent.name)
    session.refresh(agent)

    # Deliberately NOT registered in UCS here. An agent enters the
    # catalog when it is published, not when it is created — a draft is
    # work in progress, and registering one put a catalog in Explore for
    # something that may never be published, may be renamed repeatedly
    # (each rename is a different catalog), or may be deleted the same
    # minute. Publish is the point at which the agent becomes a real,
    # invocable, governable thing, so that is where it is registered
    # (see publish_agent below).
    #
    # ucs_synced stays False with no error: nothing was attempted, which
    # is different from something having failed. The UCS Access panel
    # reads it as "not registered yet".
    session.refresh(agent)
    return agent


class GenerateAgentIn(BaseModel):
    prompt: str
    name: str
    subtype: str = "Custom"
    owner: str = "you"
    app_type: str = "workflow"


@router.post("/generate", dependencies=[Depends(require_permission(P.STUDIO))])
def generate_agent(body: GenerateAgentIn, session: Session = Depends(get_session)) -> Agent:
    """Real workflow generation from a plain-language prompt (§0.46,
    GAP_REPORT) — via the coding_agent_adapter's own separate LLM
    client (mlx-lm locally, real vLLM in production; never
    model_adapter.py's Anthropic client). The generated flow is
    spliced after the SAME real, deterministic trigger/gov scaffold
    every agent gets — the model itself never authors those two nodes,
    reducing real hallucination surface to just the domain logic.
    Every terminal generated node (no further real outgoing edge) is
    wired into a real respond node for a workflow app; a chatflow app
    is left to the model's own real answer node(s).

    Always creates a real, ordinary draft agent — same status, same
    UCS sync attempt, same everything a hand-built agent gets. Never
    auto-published; the person reviews and edits this in the Builder
    like any other draft, matching this platform's own stated trust
    boundary for anything not explicitly reviewed by a human."""
    from ..coding_agent_adapter import generate_flow, CodingAgentGenerationError, CodingAgentUnavailable
    try:
        generated = generate_flow(body.prompt)
    except CodingAgentUnavailable as e:
        raise HTTPException(503, str(e))
    except CodingAgentGenerationError as e:
        raise HTTPException(422, f"Couldn't generate a real, valid workflow from that prompt: {e}")

    nodes = [{"id": "n1", "type": "trigger", "x": 40, "y": 140, "sub": "user input", "config": {}},
              {"id": "n2", "type": "gov", "x": 200, "y": 140, "sub": "entitlement gate", "config": {}}]
    edges = [{"id": "e1", "source": "n1", "target": "n2", "source_handle": None},
              {"id": "e2", "source": "n2", "target": generated["entry_node_id"], "source_handle": None}]

    gen_edges_by_source: Dict[str, List[dict]] = {}
    for e in generated["edges"]:
        gen_edges_by_source.setdefault(e["source"], []).append(e)

    x = 360
    for n in generated["nodes"]:
        nodes.append({**n, "x": x, "y": 140, "sub": ""})
        x += 160
    for i, e in enumerate(generated["edges"]):
        edges.append({"id": f"e-gen-{i}", "source": e["source"], "target": e["target"],
                       "source_handle": e.get("source_handle")})

    if body.app_type == "workflow":
        respond_id = "n-respond"
        nodes.append({"id": respond_id, "type": "respond", "x": x, "y": 140, "sub": "to user", "config": {}})
        terminal_ids = [n["id"] for n in generated["nodes"] if n["id"] not in gen_edges_by_source]
        for i, tid in enumerate(terminal_ids):
            edges.append({"id": f"e-respond-{i}", "source": tid, "target": respond_id, "source_handle": None})

    flow = {"nodes": nodes, "edges": edges}
    agent = create_agent(
        NewAgentIn(name=body.name, subtype=body.subtype, owner=body.owner, app_type=body.app_type, template_flow=flow),
        session,
    )
    return agent


class AppTypeIn(BaseModel):
    app_type: str


@router.patch("/{agent_id}/app-type", dependencies=[Depends(require_permission(P.STUDIO))])
def set_app_type(agent_id: str, body: AppTypeIn, session: Session = Depends(get_session)) -> Agent:
    VALID = {"workflow", "chatflow", "chatbot", "agent", "text_generator"}
    if body.app_type not in VALID:
        raise HTTPException(400, f"app_type must be one of {sorted(VALID)}")
    a = _get_or_404(agent_id, session)
    a.app_type = body.app_type
    a.updated_at = datetime.utcnow()
    session.add(a)
    session.commit()
    session.refresh(a)
    return a


class MetaIn(BaseModel):
    name: Optional[str] = None
    tags: Optional[List[str]] = None
    description: Optional[str] = None
    input_variables: Optional[List[dict]] = None


def _rename_in_catalog(a: Agent) -> None:
    """Push a new name to UCS, without letting the catalog block the edit.

    Same best-effort shape publish already uses for sync_agent_version: a
    rename is a local edit that must not fail because the catalog is
    unreachable, but the outcome is recorded on the agent
    (ucs_synced/ucs_sync_error) rather than swallowed, so a catalog whose
    name has drifted is visible instead of silent.
    """
    from ..ucs_sync import sync_agent, UCSSyncError
    from ..ucs_client import UCSUnavailable
    if a.status != "published":
        # A draft is not in the catalog, so there is nothing to rename
        # there. Registering it now would put the agent in Explore as a
        # side effect of an edit, which is the opposite of "only when
        # published".
        return
    try:
        sync_agent(a.id, a.name, a.description or "")
        # Register, then say what it IS. Registration carries only name and comment, so
        # without this the catalog object exists with no type, owner, status or model.
        ucs_publish.publish_all(a)
        a.ucs_synced = True
        a.ucs_sync_error = None
    except (UCSSyncError, UCSUnavailable) as e:
        a.ucs_synced = False
        a.ucs_sync_error = str(e)[:500]


@router.patch("/{agent_id}/meta", dependencies=[Depends(require_permission(P.STUDIO))])
def set_meta(agent_id: str, body: MetaIn, session: Session = Depends(get_session)) -> Agent:
    """Real tags + description, backing the Studio card grid's actual
    filter/search — not decorative UI controls with nothing behind them.
    Real `input_variables`, only meaningful for plane="Snippet" (§0.38,
    GAP_REPORT) — a snippet's own declared inputs."""
    a = _get_or_404(agent_id, session)
    if body.name is not None:
        # Renaming had no route at all: the name was fixed at creation by
        # whatever was typed into the New Agent dialog, and the Studio
        # card showed it with no way to correct a typo.
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(400, "An agent needs a name.")
        if len(new_name) > 120:
            raise HTTPException(400, "That name is too long — 120 characters at most.")
        if new_name != a.name:
            a.name = new_name
            # Role rows carry a denormalized copy, and it is CURRENT
            # configuration, not history — it is what the Grant/Revoke
            # view labels each binding with, so leaving it behind means
            # that screen names an agent that no longer exists.
            for r in session.exec(select(Role).where(Role.agent_id == a.id)).all():
                r.agent_name = new_name
                session.add(r)
            # Run, Trace and Alert also copy the name, and those are
            # deliberately NOT rewritten: each records what the agent was
            # called when it ran, which is the honest thing for a
            # historical record to say. Rewriting them would retitle the
            # past to match the present.
            _rename_in_catalog(a)
    if body.tags is not None:
        a.tags = body.tags
    if body.description is not None:
        a.description = body.description
    if body.input_variables is not None:
        a.input_variables = body.input_variables
    a.updated_at = datetime.utcnow()
    session.add(a)
    session.commit()
    session.refresh(a)
    return a


class FeaturesIn(BaseModel):
    features: dict


@router.patch("/{agent_id}/features", dependencies=[Depends(require_permission(P.STUDIO))])
def set_features(agent_id: str, body: FeaturesIn, session: Session = Depends(get_session)) -> Agent:
    """Real chat UX feature config — read by harness.py's run_flow_sync
    (moderation, follow-up, citations) and by the frontend's ChatRunner
    (conversation opener), so a developer testing in Try It sees exactly
    what an end user will get post-publish, not a separate mock.

    §0.96, GAP_REPORT — the actual fix for a real, reported gap: this
    used to directly mutate a.features in place with no real signal
    that anything had changed — draft_revision never moved, so a
    genuinely PUBLISHED agent's own production runs kept silently
    reading whatever the live features now said, even though the
    whole real point of AgentVersion (§0.54/§0.90/§0.91/§0.93) is that
    "published / vN" is a frozen, reproducible promise. This is
    unambiguously VERSIONED behavior, not operational/deployment
    configuration (see resolve_execution's own docstring,
    execution.py, for the full real category split this platform now
    follows) — moderation, chat_history, citations, and follow-ups all
    live inside this same features blob, and they all affect what a
    published version actually DOES at runtime, not just how it's
    deployed. Routes through the same real, atomic _atomic_save_agent
    every other "edit the draft" endpoint already uses — draft_revision
    genuinely increments, the same real, honest signal a node-config
    edit already gives: this change lives on the DRAFT only, and a
    real re-publish is required before it's what a production
    invocation actually resolves and executes against."""
    existing = _get_or_404(agent_id, session)
    # MERGED, not replaced. This used to assign body.features wholesale,
    # so any caller sending a partial object silently deleted every key
    # it left out. Two ways that bit:
    #
    #   * a one-key call — `{"features": {"require_authenticated_user":
    #     true}}` — wiped moderation, citations, chat history, the
    #     conversation opener and follow-ups from an agent that had them.
    #   * the Features panel builds its payload from the agent object it
    #     was handed. Try It seeds that from a snapshot taken when the
    #     page loaded, so reopening the panel without a refetch sent a
    #     STALE base: enabling one feature, closing, reopening and
    #     enabling a second replaced the first. That is the reported
    #     "enabled things are not saving" — each save was real, and the
    #     next one undid it.
    #
    # A feature is switched off by setting `enabled: false`, never by
    # omitting its key, so merging loses no ability to disable anything.
    merged = {**(existing.features or {}), **(body.features or {})}
    return _atomic_save_agent(session, agent_id, None, features=merged)


class McpToggleIn(BaseModel):
    enabled: bool


def _mcp_url(agent_id: str, token: str) -> str:
    return f"/mcp/{agent_id}/{token}"


@router.patch("/{agent_id}/mcp", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def set_mcp(agent_id: str, body: McpToggleIn, session: Session = Depends(get_session)) -> dict:
    """Real MCP Server toggle, matching Dify's own publish-mcp flow: off
    by default, and turning it on genuinely generates a fresh secret URL
    — not a static placeholder — the first time (or reuses the existing
    one if already enabled once). The URL is a real credential: anyone
    with it can call the agent through /mcp/{agent_id}/{token} below.

    Real encryption at rest (§0.59, GAP_REPORT) — the stored token is
    now encrypted; the plaintext form (needed for the actual URL a
    caller uses) is decrypted just-in-time when an existing token is
    being reused, never re-persisted anywhere.

    §0.101, GAP_REPORT — this credential now lives on real, top-level
    Agent columns (mcp_enabled/mcp_token_encrypted/...), never inside
    features["mcp"] — see Agent's own model docstring (models.py) for
    the full real reasoning: a version restore can no longer resurrect
    a rotated/revoked token, since restore only ever touches
    flow/app_type/features, never these columns."""
    import secrets
    from ..secrets_crypto import encrypt_secret, decrypt_secret
    a = _get_or_404(agent_id, session)
    a.mcp_enabled = body.enabled
    if body.enabled and not a.mcp_token_encrypted:
        plaintext_token = secrets.token_urlsafe(24)
        a.mcp_token_encrypted = encrypt_secret(plaintext_token)
        a.mcp_token_created_at = datetime.utcnow()
        session.add(AgentLog(agent_id=agent_id, ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3],
                              level="INFO", msg="MCP server token created"))
    else:
        plaintext_token = decrypt_secret(a.mcp_token_encrypted) if a.mcp_token_encrypted else None
    a.updated_at = datetime.utcnow()
    session.add(a)
    session.commit()
    session.refresh(a)
    return {"enabled": a.mcp_enabled, "url": _mcp_url(agent_id, plaintext_token) if plaintext_token else None}


@router.post("/{agent_id}/mcp/regenerate", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def regenerate_mcp_token(agent_id: str, session: Session = Depends(get_session)) -> dict:
    """Real regeneration — a genuinely new random token, and the OLD
    URL immediately stops working (checked in the real /mcp handler
    below), matching the Dify doc's own warning to treat this URL like
    an API key. Real encryption at rest (§0.59, GAP_REPORT) — same
    principle as set_mcp above.

    §0.101, GAP_REPORT — see set_mcp's own docstring just above: this
    credential is a real, top-level Agent column now, never part of
    the versioned features dict a restore could ever touch."""
    import secrets
    from ..secrets_crypto import encrypt_secret
    a = _get_or_404(agent_id, session)
    a.mcp_enabled = True
    plaintext_token = secrets.token_urlsafe(24)
    a.mcp_token_encrypted = encrypt_secret(plaintext_token)
    a.mcp_token_rotated_at = datetime.utcnow()
    a.updated_at = datetime.utcnow()
    session.add(AgentLog(agent_id=agent_id, ts=datetime.utcnow().strftime("%H:%M:%S.%f")[:-3],
                          level="INFO", msg="MCP server token rotated"))
    session.add(a)
    session.commit()
    session.refresh(a)
    return {"enabled": True, "url": _mcp_url(agent_id, plaintext_token)}


# ---------------- flow graph (builder canvas) ----------------

class EdgeIn(BaseModel):
    source: str
    target: str
    source_handle: Optional[str] = None  # e.g. "true"/"false" for a condition node's two outputs


def _parse_if_match(if_match: Optional[str]) -> Optional[int]:
    """Real format validation for the If-Match header (§0.71,
    GAP_REPORT), kept separate from the actual concurrency guard
    (§0.88, GAP_REPORT — see _atomic_save_agent's own docstring for
    why this used to be conflated with, and undermined by, a
    non-atomic Python comparison). Optional, not mandatory — a caller
    that doesn't send it (an older client, direct API automation, this
    platform's own existing tests) keeps working exactly as before,
    matching HTTP's own real If-Match semantics (a server MAY require
    it for a given resource, but a client omitting it entirely is
    always valid HTTP). A real, deliberately deferred consideration,
    not silently dropped: the report's own suggestion to eventually
    make If-Match mandatory, once every real caller has migrated to
    always sending it, remains a real, separate, future decision — not
    made here, since this fix's own real job is closing the race, not
    changing who's required to participate in it."""
    if if_match is None:
        return None
    try:
        return int(if_match)
    except (TypeError, ValueError):
        raise HTTPException(400, f"If-Match must be a real integer revision number, got {if_match!r}")


def _check_if_match(a: Agent, if_match: Optional[str]) -> Optional[int]:
    """Real, EARLY, non-authoritative fast-fail (§0.71/§0.88,
    GAP_REPORT) — kept only to reject an obviously-malformed or
    obviously-stale If-Match BEFORE a caller does other, real,
    separate work first (e.g. rollback's own real AgentVersion lookup)
    that a doomed request has no reason to pay for. This check alone
    is NOT what actually closes the real, reported race — see
    _atomic_save_agent's own docstring for why a plain Python
    comparison here, on its own, can't: two real, genuinely concurrent
    requests can both read the same a.draft_revision and both pass
    this exact check before either one's real write lands. The
    AUTHORITATIVE guard is the real, atomic `UPDATE ... WHERE
    draft_revision = :expected` every real write below now goes
    through — this function's own real 409 here is a real, honest,
    early rejection for the common, non-concurrent "you're editing a
    stale copy" case, not a substitute for the atomic one.

    Returns the real, already-parsed expected revision (or None) so a
    caller can pass it straight through to _save_flow/
    _atomic_save_agent without parsing If-Match a second time."""
    expected = _parse_if_match(if_match)
    if expected is None:
        return None
    if expected != a.draft_revision:
        raise HTTPException(
            409,
            f"This agent's flow has changed since you last loaded it (your revision {expected}, "
            f"current revision {a.draft_revision}) — reload and reapply your change.",
        )
    return expected


def _atomic_save_agent(session: Session, agent_id: str, expected_revision: Optional[int], **fields) -> Agent:
    """Real, atomic compare-and-swap (§0.88, GAP_REPORT) — the actual
    fix for a real, reported gap: `_check_if_match`'s own plain Python
    `if expected != a.draft_revision` comparison, followed later by a
    separate `a.draft_revision += 1; session.commit()`, left a real,
    genuine window open between the two real transactions doing
    exactly that:

        Tab A reads revision 10        Tab B reads revision 10
        A: If-Match=10 -> passes       B: If-Match=10 -> passes
        A writes revision 11           B writes revision 11 (last writer wins)

    Both real requests observe the SAME pre-write revision and both
    genuinely pass the same Python check — the check and the write
    were never atomic with each other, so it couldn't actually prevent
    two concurrent writers from both succeeding. A real, reported test
    gap named directly: the existing test suite only ever proved a
    STALE request (checked, THEN a different, unrelated write lands,
    THEN the stale request is retried) gets rejected — genuinely
    different from two transactions racing the SAME check concurrently,
    which is the real scenario reported here.

    The real fix: one real, atomic SQL statement —
    `UPDATE agent SET <fields>, draft_revision = draft_revision + 1,
    updated_at = now() WHERE id = :id [AND draft_revision =
    :expected] RETURNING draft_revision` — folds the check and the
    write into the SAME real database operation, using Postgres's own
    real row-level locking during the UPDATE itself (not a
    read-then-write gap this application's own Python code could ever
    leave open). If `expected_revision` is given and doesn't match,
    the WHERE clause matches zero rows — genuinely, atomically — and
    this raises a real 409, exactly the same real, honest conflict
    response as before, but now backed by a real database guarantee
    instead of a Python-level race. If `expected_revision` is None (no
    real If-Match sent), the WHERE clause omits that condition
    entirely and the write always succeeds (matching the existing,
    deliberate "optional, not mandatory" design) — but the increment
    itself (`draft_revision = draft_revision + 1`, not a Python
    read-modify-write) stays atomic either way, so two real, concurrent
    callers that both skip If-Match still can't corrupt the counter
    itself, even though a real conflict between their OWN writes isn't
    the thing being guarded against in that case.

    Re-fetches the agent with `populate_existing=True` after a
    successful write — the same real fix §0.70's own row-lock bug and
    §0.74's own HITL atomic-claim fix already needed, for the same
    real reason: a raw SQL UPDATE doesn't go through the ORM's own
    change tracking, so the caller's already-loaded Python object
    would otherwise keep showing the stale pre-write flow/revision
    even though the database itself was correctly updated."""
    values = dict(fields)
    if isinstance(values.get("flow"), dict):
        # Agent-node Env VALUES live encrypted in AgentNodeSecret; the flow
        # (and so every AgentVersion copied from it) holds key names only.
        from ..agent_assets import scrub_flow_secrets
        scrub_flow_secrets(values["flow"])
    values["draft_revision"] = Agent.draft_revision + 1
    values["updated_at"] = datetime.utcnow()
    stmt = sql_update(Agent).where(Agent.id == agent_id)
    if expected_revision is not None:
        stmt = stmt.where(Agent.draft_revision == expected_revision)
    stmt = stmt.values(**values).returning(Agent.draft_revision)
    claimed = session.exec(stmt).first()
    if claimed is None:
        session.rollback()
        current = session.get(Agent, agent_id)
        if not current:
            raise HTTPException(404, "Agent not found")
        raise HTTPException(
            409,
            f"This agent's flow has changed since you last loaded it (your revision "
            f"{expected_revision}, current revision {current.draft_revision}) — reload and reapply your change.",
        )
    session.commit()
    return session.exec(
        select(Agent).where(Agent.id == agent_id).execution_options(populate_existing=True)
    ).one()


def _save_flow(a: Agent, nodes: list, edges: list, session: Session, expected_revision: Optional[int] = None) -> Agent:
    """Deep-copy avoids aliasing the old value (the documented SQLAlchemy
    gotcha where mutating-then-reassigning a JSON column can look like a
    no-op to change detection) — still genuinely relevant here even
    though the actual write below is now a raw SQL UPDATE, not ORM
    attribute assignment (see _atomic_save_agent's own docstring):
    `nodes`/`edges` are still the caller's own live lists in some real
    call sites, and this function's own real contract is to persist an
    independent snapshot, not a live-aliased reference to them. Always
    writes both nodes AND edges together — a node-only mutation that
    forgot to pass through the existing edges would silently delete every
    connection in the flow, which is exactly the kind of gap this docstring
    exists to prevent happening again.

    §0.71/§0.88, GAP_REPORT — draft_revision is incremented atomically,
    in the SAME real SQL statement as the flow write itself (one real,
    atomic UPDATE inside _atomic_save_agent, not a separate Python
    read-modify-write), so a caller's next read always sees a revision
    number that's already accurate for the flow it's paired with —
    never a flow that changed without its own revision counter
    reflecting it, AND never two concurrent writers both succeeding
    against the same stale revision. expected_revision (the caller's
    own already-parsed If-Match value — see _parse_if_match) is passed
    straight through to the real atomic CAS; None (no real If-Match
    sent) means the write is unconditional, matching the existing,
    deliberate "optional, not mandatory" design."""
    # Bounded here, at the one chokepoint every flow write passes
    # through, and BEFORE the deep copy — so an oversized flow is
    # refused when it arrives rather than being copied, hashed, stored
    # and then re-walked by the validator, the manifest builder and
    # every run. A byte limit alone would not catch it: 50,000 nodes is
    # a few hundred kilobytes.
    check_flow_size(nodes, edges)
    new_flow = {"nodes": copy.deepcopy(nodes), "edges": copy.deepcopy(edges)}
    return _atomic_save_agent(session, a.id, expected_revision, flow=new_flow)


@router.post("/{agent_id}/nodes", dependencies=[Depends(require_permission(P.STUDIO))])
def add_node(agent_id: str, node: NodeIn, session: Session = Depends(get_session),
             if_match: Optional[str] = Header(None, alias="If-Match")) -> Agent:
    """Drop a node from the palette onto the canvas. The new node starts
    with no connections — the person draws them explicitly (§ real edge
    model, not implicit x-position wiring). Dropping it inside a loop
    container's rendered bounding box sets `parent_id`, making it one of
    that loop's real body nodes (§ real nested loop, not the body/exit-port
    workaround this used to be)."""
    if node.type not in SUPPORTED_NODE_TYPES:
        # Real, server-side type check (§0.73, GAP_REPORT) — the actual
        # fix for the reported "NodeIn.type is essentially arbitrary"
        # gap. Every real, known node type (the Builder's own palette
        # options, plus the special-cased container/hitl/comment types)
        # is accepted; anything else — a typo, a stale client, a
        # crafted request — is rejected here rather than silently
        # accepted and only failing later, silently, at execution time.
        raise HTTPException(400, f"'{node.type}' is not a real, supported node type")
    a = _get_or_404(agent_id, session)
    expected_revision = _check_if_match(a, if_match)
    nodes = copy.deepcopy(a.flow.get("nodes", []))
    edges = copy.deepcopy(a.flow.get("edges", []))
    # Real, collision-resistant node ID (§0.72, GAP_REPORT) — the real,
    # reported gap: f"n{len(nodes)+1}_{timestamp_ms % 100000}" combined
    # a value that repeats after every delete (node count) with a
    # value that wraps every 100 real seconds (timestamp mod 100000) —
    # two real, distinct nodes could genuinely land on the identical
    # id, silently colliding in the flow's own node list (a real
    # correctness bug regardless of concurrency — sequential creates
    # after a delete could collide too, not just concurrent ones). A
    # real uuid4 hex slice is what every other real id in this router
    # already uses (see e.g. KnowledgeMetadataField's "kmf-" prefix,
    # this same file's own "snip-" prefix) — 40 real bits of randomness
    # per id, a genuinely negligible collision chance even across a
    # very large real flow, not merely an improbable one.
    new_id = f"n{uuid4().hex[:10]}"
    nodes.append({"id": new_id, "type": node.type, "x": node.x, "y": node.y,
                   "sub": node.sub, "config": node.config, "parent_id": node.parent_id})
    a.updated_at = datetime.utcnow()
    return _save_flow(a, nodes, edges, session, expected_revision)


@router.patch("/{agent_id}/nodes/{node_id}/position", dependencies=[Depends(require_permission(P.STUDIO))])
def move_node(agent_id: str, node_id: str, body: NodeMoveIn, session: Session = Depends(get_session),
              if_match: Optional[str] = Header(None, alias="If-Match")) -> Agent:
    a = _get_or_404(agent_id, session)
    expected_revision = _check_if_match(a, if_match)
    nodes = copy.deepcopy(a.flow.get("nodes", []))
    edges = copy.deepcopy(a.flow.get("edges", []))
    for n in nodes:
        if n["id"] == node_id:
            # Only the trigger is fixed now. gov and respond used to be
            # here because creation placed all three and nothing could
            # add or remove them, so "fixed" and "automatic" meant the
            # same thing. Both are user-controlled now — the UCS check
            # via the toggle below, the Output node from the palette —
            # and a node someone placed themselves has to be
            # positionable, or the canvas fights them.
            if n["type"] == "trigger":
                raise HTTPException(400, "The Trigger is the fixed start of the flow and cannot be moved (§4.4)")
            n["x"], n["y"] = body.x, body.y
            break
    else:
        raise HTTPException(404, "Node not found")
    return _save_flow(a, nodes, edges, session, expected_revision)


class UcsCheckIn(BaseModel):
    enabled: bool


@router.patch("/{agent_id}/ucs-check", dependencies=[Depends(require_permission(P.STUDIO))])
def set_ucs_check_node(agent_id: str, body: UcsCheckIn, session: Session = Depends(get_session),
                        if_match: Optional[str] = Header(None, alias="If-Match")) -> Agent:
    """Show or hide the UCS check as an explicit step after the Trigger.

    IMPORTANT, and stated here because the name invites the opposite
    reading: this does NOT turn entitlement checking on or off. Whether
    or not the node is in the graph, _execute_flow resolves entitlement
    for every catalog object the run will touch before the first node
    executes (see the gate in harness.py). Authorization is not a
    feature a builder can switch off from the canvas.

    What it genuinely changes is where the check happens and whether it
    is visible as a step: with the node present the check runs at that
    point in the graph and gets its own span in the trace, which is what
    makes a denial legible when debugging; with it absent the same check
    runs as pre-flight and the canvas stays uncluttered.

    Rewiring is done here rather than in the client because it is edge
    surgery on a shared revision: inserting means re-pointing every edge
    leaving the Trigger, and removing means reconnecting the Trigger to
    whatever the check fed. Several separate calls would leave the flow
    briefly disconnected and could half-apply.
    """
    a = _get_or_404(agent_id, session)
    expected_revision = _check_if_match(a, if_match)
    nodes = copy.deepcopy(a.flow.get("nodes", []))
    edges = copy.deepcopy(a.flow.get("edges", []))

    trigger = next((n for n in nodes if n["type"] == "trigger"), None)
    gov = next((n for n in nodes if n["type"] == "gov"), None)

    if body.enabled:
        if gov:
            return a  # already a step in this flow
        if not trigger:
            raise HTTPException(400, "This flow has no Trigger, so there is no point to insert the check after.")
        gov_id = f"n{uuid4().hex[:10]}"
        # Sits between the Trigger and whatever the Trigger fed, so an
        # existing flow keeps running in the same order.
        nodes.append({"id": gov_id, "type": "gov",
                      "x": trigger["x"] + 160, "y": trigger["y"],
                      "sub": "entitlement gate", "config": {}, "parent_id": None})
        for n in nodes:
            if n["type"] not in ("trigger", "gov") and n["x"] >= trigger["x"] + 160:
                n["x"] = n["x"] + 160
        for e in edges:
            if e["source"] == trigger["id"]:
                e["source"] = gov_id
        edges.append({"id": f"e{uuid4().hex[:8]}", "source": trigger["id"],
                      "target": gov_id, "source_handle": None})
    else:
        if not gov:
            return a  # already absent
        # Reconnect across the gap rather than leaving the flow severed:
        # everything feeding the check is joined to everything it fed.
        incoming = [e for e in edges if e["target"] == gov["id"]]
        outgoing = [e for e in edges if e["source"] == gov["id"]]
        edges = [e for e in edges
                 if e["source"] != gov["id"] and e["target"] != gov["id"]]
        for inc in incoming:
            for out in outgoing:
                if not any(e["source"] == inc["source"] and e["target"] == out["target"]
                           for e in edges):
                    edges.append({"id": f"e{uuid4().hex[:8]}", "source": inc["source"],
                                  "target": out["target"],
                                  "source_handle": inc.get("source_handle")})
        nodes = [n for n in nodes if n["id"] != gov["id"]]

    a.updated_at = datetime.utcnow()
    return _save_flow(a, nodes, edges, session, expected_revision)


@router.patch("/{agent_id}/nodes/{node_id}/config", dependencies=[Depends(require_permission(P.STUDIO))])
def update_node_config(agent_id: str, node_id: str, body: NodeConfigIn, session: Session = Depends(get_session),
                        if_match: Optional[str] = Header(None, alias="If-Match")) -> Agent:
    a = _get_or_404(agent_id, session)
    expected_revision = _check_if_match(a, if_match)
    nodes = copy.deepcopy(a.flow.get("nodes", []))
    edges = copy.deepcopy(a.flow.get("edges", []))
    for n in nodes:
        if n["id"] == node_id:
            n["config"] = {**n.get("config", {}), **body.config}
            break
    else:
        raise HTTPException(404, "Node not found")
    return _save_flow(a, nodes, edges, session, expected_revision)


@router.delete("/{agent_id}/nodes/{node_id}", dependencies=[Depends(require_permission(P.STUDIO))])
def delete_node(agent_id: str, node_id: str, session: Session = Depends(get_session),
                 if_match: Optional[str] = Header(None, alias="If-Match")) -> Agent:
    a = _get_or_404(agent_id, session)
    expected_revision = _check_if_match(a, if_match)
    nodes = copy.deepcopy(a.flow.get("nodes", []))
    edges = copy.deepcopy(a.flow.get("edges", []))
    target = next((n for n in nodes if n["id"] == node_id), None)
    if not target:
        raise HTTPException(404, "Node not found")
    if target["type"] in ("trigger", "gov"):
        # The Trigger and the UCS check are placed on every canvas rather
        # than dragged in, so neither is the person's to remove. The check
        # has a switch instead (config["enabled"]) — stood down in place,
        # which leaves no gap in the graph.
        #
        # `respond` used to be in this list, correctly, while creation
        # always placed one and the palette had no entry for it. It is a
        # palette node now, so refusing to delete it meant a node you
        # could add and never remove.
        what = "Trigger" if target["type"] == "trigger" else "UCS check"
        extra = ("" if target["type"] == "trigger"
                 else " Switch it off on the node instead if you do not want it in the flow.")
        raise HTTPException(400, f"The {what} is a fixed part of every flow and cannot be deleted (§4.4).{extra}")
    # deleting a loop container also removes its real children — an
    # orphaned node with a dangling parent_id would be unreachable and
    # undeletable through the UI otherwise
    # Real, both-container-types cascade (§0.73, GAP_REPORT) — a real
    # bug found while building the flow validator: this used to check
    # `target["type"] == "loop"` only, so deleting an `iteration`
    # container left its own real children behind with a now-dangling
    # parent_id — exactly the "invalid_parent_type"/"dangling_parent"
    # case the new validator (flow_validator.py) checks for, and
    # something a person could have hit simply by deleting an
    # Iteration node from the Builder, no API misuse required.
    child_ids = {n["id"] for n in nodes if n.get("parent_id") == node_id} if target["type"] in CONTAINER_TYPES else set()
    removed_ids = {node_id} | child_ids
    nodes = [n for n in nodes if n["id"] not in removed_ids]
    # cascade: drop any edge that referenced a removed node, on either end
    edges = [e for e in edges if e["source"] not in removed_ids and e["target"] not in removed_ids]
    # The node's Env values go with it, unless a published version still has the
    # node: then they are retired (kept) so that version keeps running. This is
    # staged in the same transaction as the flow save, so both commit or neither.
    from ..agent_assets import retire_or_delete_secrets
    retire_or_delete_secrets(session, agent_id, sorted(removed_ids))
    return _save_flow(a, nodes, edges, session, expected_revision)


@router.post("/{agent_id}/edges", dependencies=[Depends(require_permission(P.STUDIO))])
def add_edge(agent_id: str, body: EdgeIn, session: Session = Depends(get_session),
             if_match: Optional[str] = Header(None, alias="If-Match")) -> Agent:
    """Draw a real connection between two nodes — the core of the drag-to-
    connect interaction. `source_handle` distinguishes a condition node's
    True/False outputs; every other node type has a single output, so it's
    omitted."""
    a = _get_or_404(agent_id, session)
    expected_revision = _check_if_match(a, if_match)
    nodes = copy.deepcopy(a.flow.get("nodes", []))
    edges = copy.deepcopy(a.flow.get("edges", []))
    node_ids = {n["id"] for n in nodes}

    if body.source not in node_ids or body.target not in node_ids:
        raise HTTPException(404, "Source or target node not found")
    if body.source == body.target:
        raise HTTPException(400, "A node cannot connect to itself")

    source_node = next(n for n in nodes if n["id"] == body.source)
    # Real, valid-handle-set-per-node-type check (§0.73, GAP_REPORT) —
    # a real bug found while building the flow validator this fixes:
    # this used to allow named handles ONLY for "condition" nodes,
    # which meant a genuinely legitimate "success"/"error" edge from a
    # code or LLM node with "Fail branch" enabled (§0.56/§0.64,
    # GAP_REPORT — a real, working feature on the config-panel and
    # execution-routing side) was REJECTED here with a real 400, even
    # though the frontend's own outputPorts() correctly offers that
    # exact port to drag from. valid_source_handles is the one, real,
    # shared source of truth for this — also used by the new
    # FlowValidator below, so the two can't independently drift again.
    allowed = valid_source_handles(source_node)
    if allowed is None:
        if body.source_handle:
            raise HTTPException(400, f"A {source_node['type']} node has a single, unnamed output — it cannot use a named handle")
    elif body.source_handle not in allowed:
        raise HTTPException(400, f"A {source_node['type']} node's edges must specify source_handle: one of {sorted(h for h in allowed if h)}")

    # Real fan-out support (§0.40, GAP_REPORT): a node's output — or a
    # condition's own True/False output, or a code node's own success/
    # error output — may now feed MORE than one downstream node. The
    # earlier "replace any existing edge from the same (source, handle)"
    # behavior is gone; dragging a second connection off an
    # already-connected port now genuinely adds a second, real,
    # independent branch rather than silently discarding the first one.
    # Still prevents a literal duplicate (same source+handle+target
    # drawn twice) rather than piling up redundant edges.
    already_exists = any(
        e["source"] == body.source and e.get("source_handle") == body.source_handle and e["target"] == body.target
        for e in edges
    )
    if not already_exists:
        # Real, collision-resistant edge ID (§0.72, GAP_REPORT) — same
        # real fix, same real reason as add_node's own new_id above.
        new_id = f"e{uuid4().hex[:10]}"
        edges.append({"id": new_id, "source": body.source, "target": body.target,
                       "source_handle": body.source_handle})
    return _save_flow(a, nodes, edges, session, expected_revision)


@router.delete("/{agent_id}/edges/{edge_id}", dependencies=[Depends(require_permission(P.STUDIO))])
def delete_edge(agent_id: str, edge_id: str, session: Session = Depends(get_session),
                 if_match: Optional[str] = Header(None, alias="If-Match")) -> Agent:
    a = _get_or_404(agent_id, session)
    expected_revision = _check_if_match(a, if_match)
    nodes = copy.deepcopy(a.flow.get("nodes", []))
    edges = copy.deepcopy(a.flow.get("edges", []))
    if not any(e["id"] == edge_id for e in edges):
        raise HTTPException(404, "Edge not found")
    edges = [e for e in edges if e["id"] != edge_id]
    return _save_flow(a, nodes, edges, session, expected_revision)


# ---------------- publish ----------------

class PublishResult(BaseModel):
    version: str
    entitlement_plan: dict
    audited_as: str = "AGENT_PUBLISH"


def _build_subagent_manifest(session: Session, flow: dict) -> dict:
    """Real Sub-Agent dependency manifest, built at publish time
    (§0.104, GAP_REPORT) — the actual fix for a real, reported
    reproducibility gap: a Sub-Agent node used to always resolve its
    target's CURRENT published AgentVersion at execution time — so an
    unchanged, already-published parent could silently invoke a
    materially different child tomorrow than it did today, the moment
    the child was independently republished. Frozen here instead, in
    the report's own recommended shape.

    For every real `subagent` node in the flow being published, resolve
    the target's CURRENT published AgentVersion — the exact same real
    lookup harness.py's own _resolve_subagent_flow already does at
    runtime today — and record its id/version. A target that's still
    only a draft at THIS moment (nothing real published to pin to yet)
    gets agent_version_id=None, an honest "unpinned" rather than a
    fabricated pin; harness.py's own runtime resolution falls back to
    resolving live at execution time for exactly this one, unavoidable
    case, unchanged from today's behavior.

    Returns {"subagents": [...]} — an empty list for a flow with no
    Sub-Agent nodes at all, never omitted, so a caller can always
    safely read manifest["subagents"] without a defensive .get."""
    entries = []
    for node in flow.get("nodes", []):
        if node.get("type") != "subagent":
            continue
        target_id = (node.get("config") or {}).get("target_agent_id")
        entry = {"node_id": node.get("id"), "agent_id": target_id, "agent_version_id": None, "version": None}
        if target_id:
            target = session.get(Agent, target_id)
            if target and target.status == "published":
                current = session.exec(
                    select(AgentVersion).where(AgentVersion.agent_id == target_id, AgentVersion.is_current == True)
                ).first()
                if current:
                    entry["agent_version_id"] = current.id
                    entry["version"] = current.version
        entries.append(entry)
    return {"subagents": entries}


def _sync_webhook_endpoints(session: Session, agent_id: str, agent_version_id: int, flow: dict) -> None:
    """Real, authoritative webhook registration, activated in the SAME
    real transaction as the AgentVersion itself (§0.97, GAP_REPORT) —
    the actual fix for a real, reported bug: webhook discovery
    (routers/webhooks.py's own _find_webhook_node) used to scan the
    live, mutable Agent.flow directly, completely independent of
    whatever flow was actually published — a Builder editing a
    trigger node's own webhook_token on the live draft, with no new
    publish, silently changed which token production responded to.
    See WebhookEndpoint's own docstring (models.py) for the full real
    design.

    Scans THIS publish's own real flow (the one just frozen into
    new_version, not the live agent.flow a second, independent time —
    though at this exact point in _resolve_and_publish they're still
    identical, reading the same local variable keeps this function
    honestly self-contained rather than implicitly depending on that)
    for every real trigger node with a configured webhook_token, and
    makes the real WebhookEndpoint rows match exactly:
    - A node whose own token is unchanged since the prior publish just
      has its agent_version_id updated, for a real, honest audit trail
      of which snapshot most recently activated it.
    - A node whose own token genuinely changed revokes the OLD
      registration (the old token must genuinely stop resolving) and
      creates a fresh one under the new token_hash.
    - A registration whose own trigger node no longer has a webhook
      token configured in THIS publish (edited or deleted) is
      genuinely revoked — it must stop resolving the moment this
      publish takes effect, not linger indefinitely.
    - §0.101, GAP_REPORT — a real, reported gap this fix also closes:
      a token restored from an OLD version (routers/agents.py's own
      restore_agent_version) and then genuinely republished could
      match a DIFFERENT, already-REVOKED WebhookEndpoint row's own
      token_hash (a real, deterministic hash — the SAME plaintext
      token always hashes the same) — the real, database-level
      unique=True constraint on token_hash then made a real, blind
      INSERT attempt genuinely fail with an IntegrityError, which the
      real, defensive AgentVersion-uniqueness catch two frames up
      wrongly reported as "a concurrent publish", confirmed live
      before this fix. Now looked up explicitly, by hash, across BOTH
      enabled and revoked rows (not just this node's own currently-
      enabled one) — a match re-activates that exact, same row rather
      than blindly trying to insert a genuine duplicate. A real,
      deliberate, different decision than the MCP credential case
      (§0.101's own primary fix): a webhook token is part of a flow's
      own explicit, versioned configuration, not a separate,
      independently-managed operational credential — a person
      explicitly restoring an old version and explicitly republishing
      it is a real, deliberate choice to bring that flow's own
      configuration back, token included, not an accidental
      resurrection with no separate publish step at all (the real,
      reported MCP case)."""
    live_node_tokens: Dict[str, str] = {}
    for node in flow.get("nodes", []):
        if node.get("type") == "trigger":
            token = (node.get("config") or {}).get("webhook_token")
            if token:
                live_node_tokens[node["id"]] = token

    existing = session.exec(
        select(WebhookEndpoint).where(WebhookEndpoint.agent_id == agent_id, WebhookEndpoint.enabled == True)
    ).all()
    existing_by_node = {e.trigger_node_id: e for e in existing}

    from .webhooks import hash_webhook_token
    now = datetime.utcnow()
    for node_id, token in live_node_tokens.items():
        token_hash = hash_webhook_token(token)
        prior_endpoint = existing_by_node.get(node_id)
        if prior_endpoint and prior_endpoint.token_hash == token_hash:
            prior_endpoint.agent_version_id = agent_version_id
            session.add(prior_endpoint)
            continue
        if prior_endpoint:
            prior_endpoint.enabled = False
            prior_endpoint.revoked_at = now
            session.add(prior_endpoint)
        # Real, explicit lookup by hash across EVERY row for this
        # agent (enabled or revoked), not just this node's own
        # currently-enabled one — the actual fix for the real,
        # reported gap above.
        reusable = session.exec(
            select(WebhookEndpoint).where(WebhookEndpoint.agent_id == agent_id, WebhookEndpoint.token_hash == token_hash)
        ).first()
        if reusable:
            reusable.trigger_node_id = node_id
            reusable.agent_version_id = agent_version_id
            reusable.enabled = True
            reusable.revoked_at = None
            session.add(reusable)
        else:
            session.add(WebhookEndpoint(
                agent_id=agent_id, agent_version_id=agent_version_id, trigger_node_id=node_id,
                token_hash=token_hash, enabled=True, created_at=now,
            ))

    for node_id, prior_endpoint in existing_by_node.items():
        if node_id not in live_node_tokens:
            prior_endpoint.enabled = False
            prior_endpoint.revoked_at = now
            session.add(prior_endpoint)


def _resolve_and_publish(a: Agent, invoking_user: str, session: Session) -> PublishResult:
    """The actual publish logic, factored out so /agents/{id}/publish and
    the templates router's "create + publish in one step" endpoint call
    the exact same real entitlement-resolution path — not two copies that
    could quietly drift apart.

    §0.70, GAP_REPORT — a real, reported race: next_v used to be
    computed from `a.version` (a plain Python read, no real locking),
    then a new AgentVersion inserted at that version and marked
    is_current — two genuinely concurrent publish calls for the SAME
    agent could both read the same starting version, both compute the
    identical next_v, and both attempt to insert it (now rejected by a
    real unique constraint — see AgentVersion's own model docstring —
    rather than silently both succeeding). Real database-level row
    locking (`SELECT ... FOR UPDATE` on this agent's own row) is what
    actually closes the race, not just detects it after the fact:
    Postgres itself makes a second, genuinely concurrent publish call
    for the same agent wait for the first to finish (commit or roll
    back) before it can even read a. version, so it always computes
    next_v from the real, post-first-publish state — a real v3 after a
    real v2, never two competing v3s."""
    from ..ucs_client import resolve_entitlement_plan, UCSUnavailable

    # Real row lock (§0.70, GAP_REPORT) — re-fetches this same agent
    # row WITH FOR UPDATE, genuinely serializing concurrent publishes
    # of the same agent at the database level. populate_existing=True
    # is the real, necessary part — without it, SQLAlchemy's own
    # identity map returns the SAME, already-cached Python object this
    # function's own caller (_get_or_404) loaded earlier via a real,
    # unlocked read, with its STALE `version` attribute still intact,
    # even though the real row lock underneath was genuinely acquired
    # correctly. A real bug caught by direct, live testing (five
    # genuinely concurrent publish calls for the same agent) before
    # this fix: the lock alone serialized the five real transactions
    # correctly, but every one of them still computed next_v from the
    # same stale, pre-lock `a.version` — the SQL sent was real and
    # correctly serialized; the Python object reading it back wasn't.
    a = session.exec(
        select(Agent).where(Agent.id == a.id).with_for_update()
        .execution_options(populate_existing=True)
    ).one()

    # Real, server-side flow validation before publish (§0.73,
    # GAP_REPORT) — the actual fix for the reported gap: a malformed
    # flow (an unsupported node type, a dangling edge, an invalid
    # handle, a missing Trigger, an unreachable step, ...) must never
    # publish, regardless of whether the request came from the
    # Builder's own UI (which already blocks this via
    # flowValidation.ts) or a raw API call that bypassed it entirely.
    validation = validate_flow(
        a.flow.get("nodes", []), a.flow.get("edges", []), a.app_type,
        is_snippet=(a.plane == "Snippet"), session=session, agent_id=a.id,
    )
    if not validation.is_valid:
        raise HTTPException(400, {
            "message": "Publish blocked — this flow has real, unresolved problems.",
            "issues": [{"severity": i.severity, "code": i.code, "message": i.message, "node_id": i.node_id} for i in validation.errors],
        })

    current = a.version.lstrip("v")
    try:
        next_v = f"v{int(current) + 1}"
    except ValueError:
        next_v = "v2"

    # Real, shared governance-object computation (§0.84, GAP_REPORT) —
    # the actual fix for the reported gap: this used to keep its own,
    # duplicate, single-KB-only computation
    # (catalog_object or knowledge_base), silently missing every real
    # object beyond the first for a real, multi-KB retrieval node — a
    # published agent's own entitlement plan could genuinely miss most
    # of what it actually touches. objects_to_check (flow_compiler.py)
    # is the one, real, shared source of truth every other real
    # governance-relevant call site already depends on.
    # governed_objects, not objects_to_check: the raw refs a Builder
    # multi-select stores are KB DISPLAY NAMES, and a name with no dots
    # can never match a UCS grant (it parses as a catalog-level
    # wildcard). Resolving them to each KB's catalog FQN is what makes
    # this check answerable at all.
    catalog_objects = governed_objects(session, a.flow.get("nodes", []))

    try:
        plan = resolve_entitlement_plan(session, a.id, a.name, invoking_user, catalog_objects)
    except UCSUnavailable as e:
        # "Couldn't ask" is not "denied", and it is certainly not a
        # bare 500 with a stack trace — which is what a real, transient
        # UCS timeout produced here: publish died on
        # ucs_client._authz_check's own 5s deadline and the person was
        # told nothing except "500". Refusing to publish is still
        # correct (an unverified entitlement must never be treated as a
        # pass), but the refusal has to say which of the two it is, and
        # that retrying is the right move.
        raise HTTPException(
            503,
            f"Publish could not verify entitlements: {e}. Nothing was published — this is a "
            f"connectivity problem with UCS, not a denial. Retry once UCS responds.",
        )
    if not plan["resolved"]:
        # An actionable refusal. This used to dump the whole plan dict,
        # which named the objects but not what was missing or how to fix
        # it — and the two halves fail for genuinely different reasons:
        # the agent principal needs a local group binding, the invoking
        # user needs to BE in a granted group. Reading a repr of nested
        # dicts to work that out is how this turned into a support
        # question every time.
        from ..ucs_provisioning import agent_default_group, auto_grant_group
        unresolved = [c for c in plan["checks"] if not c["resolved"]]
        lines = [f"Publish blocked — {len(unresolved)} object(s) this agent reads are not "
                 f"granted to it:"]
        for check in unresolved:
            missing = []
            if not check["agent_principal_ok"]:
                missing.append("the agent's service principal")
            if not check["invoking_user_ok"]:
                missing.append(f"the publishing user ({invoking_user})")
            lines.append(f"  • {check['object']} — no read grant for {' and '.join(missing)}")
        group = auto_grant_group() or agent_default_group()
        if group:
            lines.append(
                f"This deployment's configured group is '{group}'. Grant the objects to it and "
                f"make sure the publishing user is a member: "
                f"POST /api/integrations/knowledge-bases/{{kb_id}}/provision re-runs registration "
                f"and the grant for a knowledge base.")
        else:
            lines.append(
                "No UCS_AUTO_GRANT_GROUP is configured, so grants are manual here. Either set it "
                "to a custom group (new knowledge bases are then registered AND granted to it), "
                "or create the role and binding in UCS yourself.")
        lines.append(
            f"The agent's own group bindings — the agent-principal half — are managed with "
            f"POST /api/agents/{a.id}/roles {{\"group\": \"...\"}}.")
        raise HTTPException(403, "\n".join(lines))

    a.version = next_v
    a.status = "published"
    a.updated_at = datetime.utcnow()
    session.add(a)

    # Real version snapshot — the actual flow/app_type/features at this
    # exact moment, not just the bumped label. Prior versions are marked
    # not-current rather than deleted, so real history is genuinely kept.
    import copy
    for prior in session.exec(select(AgentVersion).where(AgentVersion.agent_id == a.id, AgentVersion.is_current == True)).all():
        prior.is_current = False
        session.add(prior)
    new_version = AgentVersion(
        agent_id=a.id, version=next_v, flow=copy.deepcopy(a.flow), app_type=a.app_type,
        features=copy.deepcopy(a.features or {}), entitlement_plan=plan, published_by=invoking_user,
        # Real Sub-Agent dependency manifest (§0.104, GAP_REPORT) —
        # built from the SAME real flow this AgentVersion snapshots,
        # so the manifest and the flow it describes can never drift
        # apart from each other.
        subagent_manifest=_build_subagent_manifest(session, a.flow),
        # Same precision/format as Agent.updated_at (a naive datetime,
        # isoformat()'d) — not strftime()'d to whole seconds. A real bug
        # was caught here: truncating this to seconds made it always
        # slightly LESS than the agent's own microsecond-precision
        # updated_at bumped moments earlier in this same publish call,
        # so "has anything changed since publish" was always true even
        # immediately after publishing with zero real changes.
        created_at=datetime.utcnow().isoformat(), is_current=True,
    )
    session.add(new_version)
    # Real, same-transaction id population (§0.97, GAP_REPORT) — a
    # flush (not a commit) genuinely assigns new_version's own real,
    # auto-increment id without ending this transaction — needed so
    # _sync_webhook_endpoints below can record exactly which real,
    # published snapshot activated each webhook registration, in the
    # SAME real, atomic transaction as the AgentVersion itself, per
    # the report's own recommended model ("publish AgentVersion +
    # activate webhook registrations").
    session.flush()
    _sync_webhook_endpoints(session, a.id, new_version.id, a.flow)
    # Real, defensive second layer (§0.70, GAP_REPORT) — the FOR UPDATE
    # lock above is what actually closes this race; this catch is
    # belt-and-suspenders for the same real unique constraints in case
    # a real, unexpected connection/isolation quirk ever lets a commit
    # reach here that the lock alone somehow didn't block. Rolls back
    # and reports an honest, real error rather than a raw 500 either
    # way.
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        raise HTTPException(409, f"A concurrent publish for this agent already used version {next_v} — please retry.")

    # Registering the agent in UCS happens HERE, not at creation: a
    # draft is not in the catalog at all. So this has to register the
    # Model as well as the version — the version cannot be linked to a
    # model that was never created, which is exactly the 404 this
    # produced while creation still did the registering and publish
    # assumed it had ("Model ... does not exist").
    #
    # Best-effort and non-blocking, as before: a failed or skipped sync
    # never blocks publish, and the outcome is recorded on the agent
    # rather than swallowed. The entitlement check above already gated
    # whether this publish was allowed at all.
    from ..ucs_sync import sync_agent, sync_agent_version, UCSSyncError
    from ..ucs_client import UCSUnavailable, agent_fqn
    import os as _os
    platform_base = _os.getenv("PLATFORM_BASE_URL", "http://127.0.0.1:8000")
    try:
        sync_agent(a.id, a.name, a.description or "")
        sync_agent_version(a.id, a.name, next_v, f"{platform_base}/api/agents/{a.id}/run", f"Published {next_v}")
        # Publishing is exactly when version and status change, so it is the moment the
        # catalog would otherwise go stale. Emits lineage too, which is how an agent
        # appears in UCS's graph and gets a quality score at all.
        ucs_publish.publish_all(a, run_url=f"{platform_base}/api/agents/{a.id}/run")
        a.ucs_synced = True
        a.ucs_sync_error = None
        a.ucs_fqn = agent_fqn(a.id, a.name)
    except (UCSSyncError, UCSUnavailable) as e:
        a.ucs_synced = False
        a.ucs_sync_error = str(e)[:500]
    session.add(a)
    session.commit()

    return PublishResult(version=next_v, entitlement_plan=plan)


@router.post("/{agent_id}/publish", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def publish_agent(agent_id: str, request: Request, invoking_user: Optional[str] = None,
                  session: Session = Depends(get_session)) -> PublishResult:
    """
    Calls the REAL UCS product's entitlement API (ucs_client.py) — a
    genuinely separate FastAPI service over HTTP, not a local-table stand-in.
    Publishing itself still just bumps a version pointer and flips a status
    flag (§3.6: publishing is a config change, not a deploy).
    """
    # Agent RBAC: UCS decides, this line is where the service asks.
    agent_guard.require(request, agent_id, "create_version", invoking_user=invoking_user)
    a = _get_or_404(agent_id, session)
    # A gateway-verified identity outranks anything the caller says about
    # itself; an explicit invoking_user is still honoured for a direct API
    # call. "user:demo" survives only as the last resort it always was —
    # but it is now visibly a fallback rather than the DEFAULT VALUE of the
    # parameter, which is what made every real publish ask UCS about a
    # principal that cannot hold a grant. A placeholder standing in for a
    # real identity is the same bug class as the placeholder catalog object
    # this endpoint's own entitlement check used to be handed.
    identity = get_verified_identity(request) or invoking_user or "user:demo"
    return _resolve_and_publish(a, identity, session)


class UCSGrantIn(BaseModel):
    group_name: str
    group_source: str = "custom"
    # The agent role being granted — see app/agent_roles.py. `tier` is
    # UCS's own access level and maps one-to-one onto a role; it stays
    # accepted so an older client keeps working.
    role: Optional[str] = None
    tier: Optional[str] = None


@router.post("/{agent_id}/unpublish", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def unpublish_agent(agent_id: str, session: Session = Depends(get_session)) -> dict:
    """Take a published agent off the air, returning it to draft.

    delete_agent has always told callers to "unpublish it first if that
    genuinely should be removed", but there was no unpublish anywhere in
    the product — so a published agent could not be retired or deleted
    through the API at all, and the advice pointed at nothing.

    What this genuinely stops: the agent's status, its published web-app
    and API surfaces, and every enabled WebhookEndpoint registration, so
    nothing keeps answering real traffic after the call returns.

    What it deliberately does NOT do, so the two are not confused:

      * AgentVersion history is kept, is_current included. Unpublishing
        is not "forget what was published" — republishing continues the
        version series rather than restarting it, and Run/Trace rows
        still point at the version that produced them.
      * The UCS/Gravitino Model entry is not retracted, exactly as
        delete_agent's own docstring says of deletion. Retracting a
        catalogue registration is a separate, explicit operation
        (POST /{agent_id}/ucs/revoke), and doing it implicitly here
        would silently drop grants an operator may still want.
    """
    a = _get_or_404(agent_id, session)
    if a.status == "draft":
        raise HTTPException(409, f"'{a.name}' is already a draft — there is nothing published to withdraw.")

    features = dict(a.features or {})
    publish_cfg = dict(features.get("publish") or {})
    for surface in ("web_app", "api"):
        publish_cfg[surface] = False
    features["publish"] = publish_cfg
    a.features = features
    a.status = "draft"
    a.updated_at = datetime.utcnow()
    session.add(a)

    # Webhook registrations are what actually answer inbound traffic
    # (see _sync_webhook_endpoints), so leaving them enabled would keep
    # an "unpublished" agent live on its token — the exact mismatch
    # between published state and serving state that those endpoints
    # exist to prevent.
    disabled = 0
    for ep in session.exec(select(WebhookEndpoint).where(
            WebhookEndpoint.agent_id == a.id, WebhookEndpoint.enabled == True)).all():
        ep.enabled = False
        session.add(ep)
        disabled += 1
    session.commit()
    session.refresh(a)
    return {"unpublished": True, "agent_id": a.id, "status": a.status,
            "webhook_endpoints_disabled": disabled,
            "versions_kept": True}


@router.post("/{agent_id}/ucs/sync", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def retry_ucs_sync(agent_id: str, session: Session = Depends(get_session)) -> Agent:
    """Manually retry registering this agent (and its current published
    version, if any) into UCS's real catalog — for when the automatic
    best-effort sync at creation/publish time failed, most commonly
    because the UCS gateway secret wasn't configured yet."""
    from ..ucs_sync import sync_agent, sync_agent_version, UCSSyncError
    from ..ucs_client import UCSUnavailable, agent_fqn
    import os as _os
    a = _get_or_404(agent_id, session)
    if a.status != "published":
        # Nothing to retry: a draft was never meant to be in the catalog.
        # Registering it from here would be a back door around "only
        # published agents are registered".
        raise HTTPException(
            409,
            f"'{a.name}' is a draft, so it is not registered in UCS — agents enter the "
            f"catalog when they are published. Publish it and the registration happens "
            f"as part of that.")
    try:
        sync_agent(a.id, a.name, a.description)
        platform_base = _os.getenv("PLATFORM_BASE_URL", "http://127.0.0.1:8000")
        sync_agent_version(a.id, a.name, a.version, f"{platform_base}/api/agents/{a.id}/run", f"Published {a.version}")
        ucs_publish.publish_all(a, run_url=f"{platform_base}/api/agents/{a.id}/run")
        a.ucs_synced = True
        a.ucs_sync_error = None
        a.ucs_fqn = agent_fqn(a.id, a.name)
    except (UCSSyncError, UCSUnavailable) as e:
        a.ucs_synced = False
        a.ucs_sync_error = str(e)[:500]
    session.add(a)
    session.commit()
    session.refresh(a)
    return a


@router.get("/roles/catalogue", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def agent_role_catalogue() -> List[dict]:
    """The agent roles, their permissions, and the UCS level each grants.

    Served rather than duplicated in the frontend: a role model restated
    in two places drifts, and the half that drifts is always the one
    people read.

    Each permission carries `enforced`, which is the honest part — false
    means the grant is recorded and readable but nothing consults it
    yet. A permission that gates nothing is worse than an absent one,
    because it reads as protection.
    """
    from ..agent_roles import catalogue
    return catalogue()


@router.post("/ucs/migrate-catalogs", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def migrate_agent_catalogs(session: Session = Depends(get_session)) -> dict:
    """Move every agent into its own UCS catalog, carrying grants across.

    One-shot for an existing deployment: agents registered before this
    change live as models inside the shared `agent_catalog.agents`, and
    their roles are scoped to that old FQN — which would grant nothing
    once each agent is governed under its own catalog. See
    ucs_sync.migrate_agent_to_own_catalog.

    Safe to re-run: registration and re-granting are both idempotent.
    """
    from ..ucs_sync import migrate_agent_to_own_catalog, UCSSyncError
    from ..ucs_client import UCSUnavailable, agent_fqn
    import os as _os
    platform_base = _os.getenv("PLATFORM_BASE_URL", "http://127.0.0.1:8000")

    results = []
    for a in session.exec(select(Agent)).all():
        if a.status != "published":
            # Drafts are not in the catalog at all, so there is nothing
            # to move — and registering one here would reintroduce
            # exactly what "only published agents are registered" rules
            # out.
            results.append({"agent": a.name, "agent_id": a.id, "skipped": "draft"})
            continue
        try:
            r = migrate_agent_to_own_catalog(
                a.id, a.name, a.version,
                f"{platform_base}/api/agents/{a.id}/run")
            a.ucs_fqn = agent_fqn(a.id, a.name)
            a.ucs_synced = True
            a.ucs_sync_error = None
            results.append({"agent": a.name, **r})
        except (UCSSyncError, UCSUnavailable) as e:
            a.ucs_synced = False
            a.ucs_sync_error = str(e)[:500]
            results.append({"agent": a.name, "agent_id": a.id, "error": str(e)[:300]})
        session.add(a)
    session.commit()
    return {"agents": len(results), "results": results}


@router.post("/{agent_id}/ucs/grant", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def grant_ucs_access(agent_id: str, body: UCSGrantIn, request: Request,
                     session: Session = Depends(get_session)) -> dict:
    """Real access grant, via UCS's real admin API — creates a real role
    scoped to this agent's real Model object and binds a real group to
    it. This is genuinely the same mechanism as every other UCS
    entitlement in this platform; there's no local ACL table here that
    could drift out of sync with what UCS actually enforces."""
    # Agent RBAC: UCS decides, this line is where the service asks.
    agent_guard.require(request, agent_id, "manage_entitlements")
    from ..ucs_sync import grant_agent_access, UCSSyncError
    from ..ucs_client import UCSUnavailable
    from ..agent_roles import ROLES, DEFAULT_ROLE, canonical_role, role_for_tier, tier_for_role
    a = _get_or_404(agent_id, session)

    # One of the two is authoritative, never both: a caller sending a
    # role gets the tier that role maps to, and an older caller sending
    # only a tier is mapped to its role. Honouring both independently
    # would let a request ask for USE_AGENT with owner rights. A legacy
    # five-role name (agent_user, ...) is accepted and mapped.
    if body.role:
        role_name = canonical_role(body.role)
        if role_name not in ROLES:
            raise HTTPException(
                400, f"Unknown role {body.role!r}. Valid roles: {', '.join(ROLES)}")
    elif body.tier:
        role_name = role_for_tier(body.tier)
    else:
        role_name = DEFAULT_ROLE
    tier = tier_for_role(role_name)

    if tier == "owner":
        # CREATE_AGENT is granted on the agent's catalog schema (UCS cannot
        # grant it on the agent itself), and catalogs are named after the
        # agent — so a same-named agent would be covered by this grant too.
        from ..ucs_client import agent_slug
        slug = agent_slug(a.name)
        twins = [o.name for o in session.exec(select(Agent).where(Agent.id != a.id)).all()
                 if agent_slug(o.name) == slug]
        if twins:
            raise HTTPException(
                409, f"Can't grant CREATE_AGENT: another agent named {twins[0]!r} shares this "
                     f"agent's UCS catalog '{slug}', so the grant would cover it too. "
                     "Rename one of them first.")

    try:
        result = grant_agent_access(a.id, a.name, body.group_name, body.group_source,
                                    tier, role=role_name)
    except (UCSSyncError, UCSUnavailable) as e:
        raise HTTPException(400, f"Grant failed — {e}")

    # Record which role this grant represents, so the binding reads back
    # under the same name UCS shows.
    existing = session.exec(
        select(Role).where(Role.agent_id == a.id, Role.group == body.group_name)
    ).first()
    if existing:
        existing.agent_role = role_name
        existing.tier = tier
        session.add(existing)
    else:
        session.add(Role(agent_id=a.id, agent_name=a.name, tier=tier,
                         agent_role=role_name, group=body.group_name))
    session.commit()

    return {**(result or {}), "role": role_name, "ucs_access_level": tier}


@router.post("/{agent_id}/ucs/revoke", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def revoke_ucs_access(agent_id: str, body: UCSGrantIn, request: Request,
                      session: Session = Depends(get_session)) -> dict:
    # Agent RBAC: UCS decides, this line is where the service asks.
    agent_guard.require(request, agent_id, "manage_entitlements")
    from ..ucs_sync import revoke_agent_access, UCSSyncError
    from ..ucs_client import UCSUnavailable
    from ..agent_roles import tier_for_role
    a = _get_or_404(agent_id, session)
    # The Builder sends the role, not the tier; revoking with tier=None
    # used to target a UCS role named "agent-<id>-None" and remove nothing.
    tier = body.tier or (tier_for_role(body.role) if body.role else "select")
    try:
        result = revoke_agent_access(a.id, body.group_name, tier, body.group_source)
    except (UCSSyncError, UCSUnavailable) as e:
        raise HTTPException(400, f"Revoke failed — {e}")
    return result


@router.get("/{agent_id}/versions", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def list_agent_versions(agent_id: str, session: Session = Depends(get_session)) -> List[AgentVersion]:
    """Real version history — every past publish snapshot, most recent
    first, not a synthetic list derived from just the current version
    string."""
    return session.exec(
        select(AgentVersion).where(AgentVersion.agent_id == agent_id).order_by(AgentVersion.id.desc())
    ).all()


@router.post("/{agent_id}/versions/{version_id}/restore", dependencies=[Depends(require_permission(P.STUDIO))])
def restore_agent_version(agent_id: str, version_id: int, request: Request,
                           session: Session = Depends(get_session),
                           if_match: Optional[str] = Header(None, alias="If-Match")) -> Agent:
    """Real rollback — genuinely replaces the agent's live flow/app_type/
    features with a prior snapshot's real values, not just relabeling the
    version string. The restored state becomes a NEW draft (not silently
    marked as still that old version), matching how a real rollback is
    itself a real change that would need its own re-publish to go live —
    it doesn't retroactively pretend nothing happened in between.

    §0.71, GAP_REPORT — a real flow mutation like any other Builder
    change, so it goes through the same real If-Match check and bumps
    draft_revision the same way — a concurrent Builder edit racing a
    rollback is exactly the kind of conflict this fix exists for."""
    # Agent RBAC: UCS decides, this line is where the service asks.
    agent_guard.require(request, agent_id, "manage_versions")
    import copy
    a = _get_or_404(agent_id, session)
    expected_revision = _check_if_match(a, if_match)
    v = session.get(AgentVersion, version_id)
    if not v or v.agent_id != agent_id:
        raise HTTPException(404, "Version not found")
    # Assets and Env values this version used may have been retired since; bring
    # them back with the draft so the Configure panel shows what the nodes use.
    from ..agent_assets import reactivate_for_flow
    reactivate_for_flow(session, agent_id, v.flow)
    return _atomic_save_agent(
        session, a.id, expected_revision,
        flow=copy.deepcopy(v.flow), app_type=v.app_type,
        features=copy.deepcopy(v.features or {}), status="draft",
    )


# ---------------- sub-resources ----------------

@router.get("/{agent_id}/chat-history", dependencies=[Depends(require_any_permission(*P.AGENT_INVOKERS))])
def get_chat_history(agent_id: str, invoking_user: str,
                     session: Session = Depends(get_session)) -> List[dict]:
    """The stored transcript for one (agent, invoking_user) pair.

    These rows already existed and were already being read — by
    harness._load_chat_history, which hands prior turns to the model so
    it genuinely remembers a conversation (§0.47). Nothing exposed them
    to a client, so closing the web app and reopening it showed an
    empty transcript while the agent carried on as though the earlier
    turns had happened. Losing the display but keeping the model's
    memory is worse than losing both: the agent answers questions the
    person can no longer see it was asked.

    Ordered oldest-first, which is the order a transcript renders in,
    rather than _load_chat_history's newest-first-then-reversed shape.
    Capped so reopening a long-running conversation cannot return an
    unbounded page.
    """
    _get_or_404(agent_id, session)
    rows = session.exec(
        select(ChatMessage)
        .where(ChatMessage.agent_id == agent_id,
               ChatMessage.invoking_user == invoking_user)
        .order_by(ChatMessage.created_at.desc())
        .limit(200)
    ).all()
    return [
        {"role": r.role, "content": r.content,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in reversed(rows)
    ]


@router.get("/{agent_id}/logs", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def get_agent_logs(
    agent_id: str, response: Response, session: Session = Depends(get_session),
    page: dict = Depends(pagination_params),
) -> List[AgentLog]:
    """Most-recent-first — without this, a real new log from an actual run
    could land anywhere among older/seeded rows instead of at the top,
    which is exactly backwards for a "recent activity" view. Found by
    testing that a real user's message actually surfaces where you'd
    look for it, not just that it was written to the table at all.

    §0.113, GAP_REPORT — the same real, keyset-pagination fix as
    runs.py/traces.py's own identical gap; see pagination.py's own
    module docstring for the full real reasoning. AgentLog has no real
    created_at timestamp column of its own — its own real, monotonic
    integer `id` (a real Postgres SERIAL, genuinely assigned in real
    insertion order) is already a stable total order on its own, so
    the exact same real `paginate` helper is used with `sort_col`
    and `id_col` both set to `AgentLog.id`."""
    q = select(AgentLog).where(AgentLog.agent_id == agent_id)
    return paginate(session, AgentLog, q, limit=page["limit"], cursor=page["cursor"],
                     sort_col=AgentLog.id, id_col=AgentLog.id, response=response)


class AgentGroupBindingIn(BaseModel):
    group: str
    tier: str = "select"


@router.post("/{agent_id}/roles", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def bind_agent_role(agent_id: str, body: AgentGroupBindingIn,
                    session: Session = Depends(get_session)) -> dict:
    """Bind this agent's service principal to a group.

    There was no way to do this through the product at all — the `role`
    table was written only by seed.py, and its model docstring said
    bindings "are administered in UCS's own console… no create/update/
    delete endpoint exists here on purpose". But nothing ever synced them
    FROM UCS either, so an agent created through the product had no
    groups and the agent-principal half of its publish entitlement check
    could never pass. Closing that with SQL by hand was the only option.

    The binding is local because that is where the check reads it; the
    GRANT itself still lives in UCS and is not created here.
    """
    agent = _get_or_404(agent_id, session)
    if body.tier not in ("select", "writer", "owner"):
        raise HTTPException(400, "tier must be one of select, writer, owner")
    from ..ucs_provisioning import bind_agent_group
    return bind_agent_group(session, agent.id, agent.name, body.group.strip(), body.tier)


@router.delete("/{agent_id}/roles/{group}", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def unbind_agent_role(agent_id: str, group: str, tier: str = "select",
                      session: Session = Depends(get_session)) -> dict:
    """Remove a group binding. Revoking the UCS grant is separate — this
    only stops this agent claiming that group."""
    _get_or_404(agent_id, session)
    row = session.exec(
        select(Role).where(Role.agent_id == agent_id, Role.group == group, Role.tier == tier)
    ).first()
    if not row:
        raise HTTPException(404, "No such group binding for this agent")
    session.delete(row)
    session.commit()
    return {"unbound": True, "agent_id": agent_id, "group": group, "tier": tier}


@router.get("/{agent_id}/roles", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def get_agent_roles(agent_id: str, session: Session = Depends(get_session)) -> List[Role]:
    """Read-only. Bindings are administered in UCS's own console, not here (§7.4/§7.5)."""
    return session.exec(select(Role).where(Role.agent_id == agent_id)).all()


@router.get("/{agent_id}/traces", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def get_agent_traces(
    agent_id: str, response: Response, session: Session = Depends(get_session),
    page: dict = Depends(pagination_params),
) -> List[Trace]:
    """§0.113, GAP_REPORT — the same real, keyset-pagination fix as
    traces.py's own top-level /api/traces endpoint (identical real
    gap, just agent-scoped here)."""
    q = select(Trace).where(Trace.agent_id == agent_id)
    return paginate(session, Trace, q, limit=page["limit"], cursor=page["cursor"],
                     sort_col=Trace.started_at, id_col=Trace.id, response=response)


def _get_or_404(agent_id: str, session: Session) -> Agent:
    a = session.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent not found")
    return a


@router.get("/{agent_id}/dsl", dependencies=[Depends(require_any_permission(*P.AGENT_READERS))])
def export_dsl(agent_id: str, session: Session = Depends(get_session)) -> dict:
    """Real DSL export — the agent's actual flow, name, subtype, and
    app_type, not a stub. What comes back here is exactly what
    import_dsl below can turn back into a real running agent."""
    a = _get_or_404(agent_id, session)
    return {
        "dsl_version": "1", "name": a.name, "subtype": a.subtype, "type": a.type,
        "app_type": a.app_type, "description": a.description, "flow": a.flow,
    }


class ImportDslIn(BaseModel):
    dsl: dict
    as_new_agent: bool = True
    target_agent_id: Optional[str] = None  # required if as_new_agent is False


@router.post("/import-dsl", dependencies=[Depends(require_permission(P.STUDIO))])
def import_dsl(body: ImportDslIn, session: Session = Depends(get_session),
                if_match: Optional[str] = Header(None, alias="If-Match")) -> Agent:
    """Real DSL import. Creates a genuinely new agent from the uploaded
    flow by default (as_new_agent=True) — or, if a target_agent_id is
    given, REPLACES that agent's real flow in place. Either way the
    resulting agent actually runs the imported flow; this isn't a
    metadata-only stub.

    §0.71, GAP_REPORT — the real If-Match check and draft_revision
    bump only apply to the REPLACE-existing-agent path; a brand new
    agent has no prior revision for a caller to have raced against."""
    dsl = body.dsl or {}
    flow = dsl.get("flow") or {"nodes": [], "edges": []}
    if body.as_new_agent or not body.target_agent_id:
        new_id = f"a{uuid4().hex[:10]}"
        agent = Agent(
            id=new_id, name=dsl.get("name") or "Imported agent", type="App",
            subtype=dsl.get("subtype") or "Custom", plane="App", owner="you", status="draft",
            version="v1", health_status="—", flow=flow, metrics={},
            app_type=dsl.get("app_type"), description=dsl.get("description") or "",
        )
        session.add(agent)
    else:
        agent = _get_or_404(body.target_agent_id, session)
        expected_revision = _check_if_match(agent, if_match)
        return _atomic_save_agent(session, agent.id, expected_revision, flow=flow)
    session.commit()
    # The same default group binding create_agent applies — an imported
    # agent with no groups fails the agent-principal half of its own
    # entitlement check on every governed object it touches.
    from ..ucs_provisioning import bind_agent_to_default_group
    bind_agent_to_default_group(session, agent.id, agent.name)
    session.refresh(agent)
    return agent


@router.post("/{agent_id}/duplicate", dependencies=[Depends(require_permission(P.STUDIO))])
def duplicate_agent(agent_id: str, session: Session = Depends(get_session)) -> Agent:
    """Real clone — genuinely copies the source agent's real flow,
    features, and metadata into a brand new draft agent with its own
    id, not a shared reference (editing the copy never mutates the
    original)."""
    import copy
    src = _get_or_404(agent_id, session)
    new_id = f"a{uuid4().hex[:10]}"
    clone = Agent(
        id=new_id, name=f"{src.name} (Copy)", type=src.type, subtype=src.subtype,
        plane=src.plane, owner=src.owner, status="draft", version="v1", health_status="—",
        flow=copy.deepcopy(src.flow), metrics={}, app_type=src.app_type,
        description=src.description, tags=list(src.tags or []),
        features=copy.deepcopy({k: v for k, v in (src.features or {}).items() if k != "mcp"}),
    )
    session.add(clone)
    session.flush()

    # Skills and reference files are per-agent: copy them (rows + bytes) and
    # remap the ids inside the cloned flow. If any copy fails the whole
    # duplicate is rolled back, so a clone never shares, or points at missing,
    # source bytes. Env values are not copied: the clone gets the key names
    # and the person re-enters the values.
    from ..agent_assets import copy_assets_for_agent, AssetError, delete_blobs
    new_flow = copy.deepcopy(clone.flow)
    written: list = []
    try:
        written = copy_assets_for_agent(session, src.id, clone.id, new_flow)
        clone.flow = new_flow
        session.add(clone)
        session.commit()
    except AssetError as e:
        session.rollback()
        raise HTTPException(e.status, f"Could not duplicate this agent: {e.reason}")
    except Exception:
        session.rollback()
        delete_blobs(written)  # the copies were written but the commit failed
        raise

    # A duplicate inherits the source's group bindings, and falls back to
    # the deployment default when the source has none. Without this the
    # clone had no groups at all, so a copy of a working agent was denied
    # on every governed object the original could read — a duplicate that
    # cannot do what it was duplicated from.
    #
    # Not an escalation: these are local claims to the same groups, and
    # UCS still decides whether those groups actually hold the grant.
    source_roles = session.exec(select(Role).where(Role.agent_id == src.id)).all()
    if source_roles:
        for r in source_roles:
            session.add(Role(agent_id=clone.id, agent_name=clone.name,
                             group=r.group, tier=r.tier))
        session.commit()
    else:
        from ..ucs_provisioning import bind_agent_to_default_group
        bind_agent_to_default_group(session, clone.id, clone.name)

    session.refresh(clone)
    return clone


class InsertSnippetIn(BaseModel):
    snippet_id: str
    x: float = 40
    y: float = 40


@router.post("/{agent_id}/insert-snippet", dependencies=[Depends(require_permission(P.STUDIO))])
def insert_snippet(agent_id: str, body: InsertSnippetIn, session: Session = Depends(get_session),
                    if_match: Optional[str] = Header(None, alias="If-Match")) -> Agent:
    """Real "Add a Snippet to a Workflow" (§0.38, GAP_REPORT, matching
    Dify's own documented behavior exactly): genuinely copies the real
    published snippet's real nodes/edges into the target agent's real
    flow — not a reference, not a symbolic "snippet instance" node.
    Editing the original snippet later never changes a workflow that
    already used it, and vice versa, matching Dify's own stated
    behavior, because this is a genuine, independent deep copy the
    moment it's inserted.

    Every copied node gets a real NEW id (prefixed `snip-`, globally
    unique via uuid4) so it can never collide with the target flow's
    existing node ids even if the same snippet is inserted twice, or
    inserted into a flow that already contains a copy of it. Internal
    edges between the snippet's own nodes are real too — their
    source/target ids are rewritten to the new ids, preserving the
    real internal wiring; edges pointing outside the copied set (there
    shouldn't be any, since a snippet's nodes are only ever connected
    to each other) are dropped defensively rather than left dangling.

    Real position offset: the snippet's own nodes may have been laid
    out anywhere on ITS OWN canvas; every copied node is shifted so its
    real bounding box's top-left corner lands at the real (x, y) the
    caller provided (e.g., wherever a person right-clicked on the
    target canvas), not pasted at each node's original absolute
    coordinates."""
    target = _get_or_404(agent_id, session)
    expected_revision = _check_if_match(target, if_match)
    snippet = session.get(Agent, body.snippet_id)
    if not snippet:
        raise HTTPException(404, "Snippet not found")
    if snippet.plane != "Snippet":
        raise HTTPException(400, "That agent isn't a snippet")
    if snippet.status != "published":
        raise HTTPException(400, "Only a published snippet can be inserted — publish it first")

    src_nodes = copy.deepcopy(snippet.flow.get("nodes", []))
    src_edges = copy.deepcopy(snippet.flow.get("edges", []))
    if not src_nodes:
        raise HTTPException(400, "This snippet has no nodes to insert")

    min_x = min(n.get("x", 0) for n in src_nodes)
    min_y = min(n.get("y", 0) for n in src_nodes)
    # Real two-pass remap: build the COMPLETE old-id -> new-id map first,
    # so a child node's real parent_id resolves correctly regardless of
    # which order the nodes happen to appear in (not guaranteed to be
    # parent-before-child).
    id_map: dict[str, str] = {n["id"]: f"snip-{uuid4().hex[:10]}" for n in src_nodes}
    new_nodes = []
    for n in src_nodes:
        new_node = dict(n)
        new_node["id"] = id_map[n["id"]]
        new_node["x"] = n.get("x", 0) - min_x + body.x
        new_node["y"] = n.get("y", 0) - min_y + body.y
        if n.get("parent_id") and n["parent_id"] in id_map:
            new_node["parent_id"] = id_map[n["parent_id"]]
        new_nodes.append(new_node)

    new_edges = []
    for e in src_edges:
        if e.get("source") in id_map and e.get("target") in id_map:
            new_edge = dict(e)
            new_edge["id"] = f"snip-e-{uuid4().hex[:10]}"
            new_edge["source"] = id_map[e["source"]]
            new_edge["target"] = id_map[e["target"]]
            new_edges.append(new_edge)

    flow = dict(target.flow)
    flow["nodes"] = list(flow.get("nodes", [])) + new_nodes
    flow["edges"] = list(flow.get("edges", [])) + new_edges
    return _atomic_save_agent(session, target.id, expected_revision, flow=flow)


class PublishSurfaceIn(BaseModel):
    surface: str  # "web_app" | "api"
    enabled: bool


@router.patch("/{agent_id}/publish-surface", dependencies=[Depends(require_permission(P.AGENTS_PUBLISH))])
def set_publish_surface(agent_id: str, body: PublishSurfaceIn, session: Session = Depends(get_session)) -> Agent:
    """Real, independently-toggleable publish surfaces, matching Dify's
    own per-service IN SERVICE / DISABLED state — the Web App and the
    Backend Service API can genuinely be enabled independently, not
    coupled to one shared status flag."""
    if body.surface not in ("web_app", "api"):
        raise HTTPException(400, "surface must be 'web_app' or 'api'")
    a = _get_or_404(agent_id, session)
    features = dict(a.features or {})
    publish_cfg = dict(features.get("publish") or {})
    publish_cfg[body.surface] = body.enabled
    features["publish"] = publish_cfg
    a.features = features
    a.updated_at = datetime.utcnow()
    session.add(a)
    session.commit()
    session.refresh(a)
    return a
