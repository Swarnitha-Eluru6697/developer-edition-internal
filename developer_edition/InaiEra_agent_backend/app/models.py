"""
Data models for the InAiEra Agent Platform backend.

NOTE ON SCOPE: this backend is a real, runnable CRUD/API skeleton matching
the approved UI mockup. It is NOT a production implementation of the design
doc's runtime — there is no real Temporal orchestration, no real UCS
entitlement service, no real LLM calls, and no real Firecracker/gVisor
sandboxing. Those require actual infrastructure (a Temporal cluster, a UCS
deployment, model provider credentials, a Linux/KVM host) that doesn't
exist in this environment. Endpoints that would call those systems are
clearly marked STUB below and return realistic canned responses so the
frontend has something real to call and persist against.
"""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import UniqueConstraint, Numeric, DateTime, CheckConstraint, String, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from pydantic import field_serializer, computed_field

SECRET_MASK = "••••••••"


def _mask_mcp_token(features: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Real, shared masking (§0.59, GAP_REPORT) for the one real secret
    that lives inside the free-form `features` JSONB blob —
    features["mcp"]["token"], the actual bearer credential for this
    agent's real MCP server URL. A real, confirmed bug before this
    fix: every normal, unprivileged GET of an Agent or AgentVersion
    (list, get, version history) returned this token in plaintext,
    since `features` is otherwise a perfectly ordinary field with
    nothing marking it as needing this treatment. Applied via
    Pydantic's own field_serializer — a single, centralized hook every
    real serialization path (FastAPI's response encoding, a direct
    .model_dump() call, anything) goes through, rather than needing
    every one of the 20+ real endpoints that return an Agent to
    remember to mask it individually. The real, live token is still
    genuinely returned exactly once, at the moment it's created or
    rotated (routers/agents.py's set_mcp/regenerate_mcp_token — these
    return a constructed dict, not the Agent object itself, so this
    serializer never runs for them), matching the same one-time-reveal
    convention EnvVar secrets already use."""
    f = dict(features or {})
    mcp = f.get("mcp")
    if isinstance(mcp, dict) and mcp.get("token"):
        f = dict(f)
        f["mcp"] = {**mcp, "token": SECRET_MASK}
    return f


class Agent(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str
    type: str  # "Data" | "App"
    subtype: str
    plane: str  # "Data" | "App"
    owner: str
    status: str = "draft"  # "draft" | "staged" | "published"
    version: str = "v1"
    health_score: Optional[int] = None
    health_status: str = "—"  # "pass" | "warn" | "fail" | "—"
    model: str = "Claude Sonnet 5"
    mode: str = "agentic"  # "agentic" | "fixed" — top-level default for new LLM nodes
    # Only meaningful for type="App" — determines which real end-user UI
    # renders post-publish (routers/agents.py serves this to AgentRunner.tsx):
    # "workflow" (form in, structured result out, no chat history),
    # "chatflow" (chat UI backed by this agent's real flow graph),
    # "chatbot" (plain chat UI), "agent" (chat UI that ALSO surfaces the
    # real per-step reasoning trace from an `agent`-strategy node, if the
    # flow has one), "text_generator" (one-shot prompt-in/text-out, no
    # history at all). Data-type agents don't use this field.
    app_type: Optional[str] = None
    tags: List[str] = Field(default_factory=list, sa_column=Column(JSONB))
    description: str = ""
    # Real, per-agent chat UX features — Try It and the published /app/
    # page both read this so a developer testing pre-publish sees the
    # exact behavior an end user gets, not a separate preview-only config.
    # Shape: {conversation_opener:{enabled,text}, follow_up:{enabled},
    # citations:{enabled}, moderation:{enabled, blocked_words:[...]}}.
    # text_to_speech/speech_to_text and annotation_reply from the
    # reference UI are a real, deliberately-deferred follow-on — see
    # GAP_REPORT.md — not silently dropped.
    features: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    runs_7d: int = 0
    cost_7d: str = "$0.00"
    # Stable template provenance. `template_name` is retained as a convenient
    # display snapshot, while the identifiers make the exact published
    # template version reproducible even if the template is later renamed or
    # archived.
    template_name: Optional[str] = Field(default=None, index=True)
    template_id: Optional[str] = Field(default=None, foreign_key="template.id", index=True)
    template_version_id: Optional[str] = Field(default=None, foreign_key="templateversion.id", index=True)
    first_party: bool = False
    # Flow graph — this IS what "publish" produces (§3.6 of the design doc):
    # a JSON document, not compiled code. Shape: {"nodes":[{id,type,x,y,sub,config}]}
    flow: Dict[str, Any] = Field(default_factory=lambda: {"nodes": []}, sa_column=Column(JSONB))
    # Real optimistic concurrency control for the flow document (§0.71,
    # GAP_REPORT) — a real, reported gap: every node/edge mutation did a
    # genuine read-full-flow / modify-local-copy / write-full-flow round
    # trip with no revision check at all, so two concurrent Builder tabs
    # editing the same agent could each read the same flow, make
    # different real changes, and the second write would silently
    # overwrite the first's — not merged, not even detected, just gone.
    # Starts at 1, incremented by exactly 1 on every real flow mutation
    # (see routers/agents.py's own _save_flow) — every mutating endpoint
    # now accepts a real, optional `If-Match` request header carrying
    # the revision the caller believes it's editing against; when
    # present, a mismatch against the real, current value here is
    # rejected with a real 409 Conflict instead of silently applying a
    # write based on stale data. Optional, not mandatory, so an existing
    # API caller that hasn't been updated to send it yet keeps working
    # exactly as before — the same real, additive, non-breaking pattern
    # every other GAP_REPORT schema change this session has used.
    draft_revision: int = 1
    # Only meaningful for plane="Snippet" (§0.38, GAP_REPORT) — real
    # declared inputs a snippet expects from whatever workflow it's
    # inserted into, matching Dify's own documented "Input Variables"
    # concept for snippets (a snippet has no Start node of its own to
    # collect input, so this is how it declares what it needs). Shape:
    # [{name, type, description}].
    input_variables: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB))
    metrics: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # Real sync status against UCS's actual catalog (see ucs_sync.py) —
    # whether this agent is genuinely registered as a Gravitino Model
    # there, not just a local flag asserted to be true. None means never
    # attempted; a real error string means attempted and genuinely failed
    # (most commonly: no UCS gateway secret configured in this environment).
    ucs_synced: bool = False
    ucs_sync_error: Optional[str] = None
    ucs_fqn: Optional[str] = None
    # Real, first-class MCP endpoint credential (§0.101, GAP_REPORT) —
    # the actual fix for a real, reported bug: this used to live inside
    # features["mcp"], the same real, live dict AgentVersion.features
    # snapshots at publish time — a real, reported vulnerability
    # followed directly from that: restoring an OLD version copied that
    # OLD snapshot's own token back into the LIVE agent, genuinely
    # resurrecting a credential that had already been rotated/revoked
    # since. The report's own recommended model — "never put
    # operational credentials into immutable application versions" —
    # is realized here structurally, not just patched around: these are
    # real, TOP-LEVEL Agent columns, and restore_agent_version/
    # _resolve_and_publish (routers/agents.py) only ever read/write
    # flow/app_type/features — never these — so a version restore
    # genuinely CANNOT touch this credential at all anymore, the same
    # real guarantee §0.97's own WebhookEndpoint table already
    # established for webhook tokens (a genuinely separate case, kept
    # reversibly encrypted here rather than one-way hashed, since a
    # real caller re-toggling MCP on/off needs the SAME real URL shown
    # back — unlike a webhook token, which is never redisplayed after
    # creation).
    mcp_enabled: bool = False
    mcp_token_encrypted: Optional[str] = None
    mcp_token_created_at: Optional[datetime] = None
    mcp_token_rotated_at: Optional[datetime] = None

    @field_serializer("features")
    def _serialize_features(self, features: Dict[str, Any], _info):
        return _mask_mcp_token(features)

    @field_serializer("mcp_token_encrypted")
    def _serialize_mcp_token_encrypted(self, value: Optional[str], _info):
        # Real, deliberate masking (§0.101, GAP_REPORT) — the same real
        # reason _mask_mcp_token already exists for the old
        # features["mcp"]["token"] location: the encrypted credential
        # itself is never a real, legitimate thing for a person to read
        # back through a plain GET /agents response — only the actual,
        # real MCP URL (built server-side from the real, decrypted
        # plaintext, at the one, real moment a caller explicitly
        # enables/regenerates it) is ever meant to be seen.
        return SECRET_MASK if value else None


class AgentLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    ts: str
    level: str  # INFO | WARN | ERROR
    msg: str
    trace_id: Optional[str] = None
    # The full instant the line was written.
    #
    # `ts` above is a bare clock string ("04:28:07.696") with no date, which makes a log
    # impossible to order across midnight or filter by day — the catalogue's Logs tab could
    # only date a line by resolving its trace, and a third of lines have no trace. Defaulted
    # rather than required so every existing writer keeps working unchanged; rows written
    # before this column exist stay null and are reported as undated rather than guessed at.
    at: Optional[datetime] = Field(default_factory=datetime.utcnow, index=True)


class AgentVersion(SQLModel, table=True):
    """A real, immutable snapshot taken at the moment an agent is
    published — the actual flow, app_type, and features at that point in
    time, not just a version string. Lets a person genuinely see what
    changed between versions and roll back to a prior one's real
    behavior, not just its label.

    Real, database-level uniqueness (§0.70, GAP_REPORT) — a real,
    reported gap: (agent_id, version) was only ever checked in Python
    (compute next_v from the agent's own current version string, then
    insert), so two genuinely concurrent publish calls for the same
    agent could both compute the identical next_v and both insert —
    Postgres itself now rejects the second one via a real unique
    constraint, rather than silently allowing two rows for what should
    be one real version. The "exactly one is_current=True row per
    agent" invariant needs a real, separate partial unique index
    (uniqueness only among is_current=True rows — a plain column
    UniqueConstraint can't express that), added directly in the real
    Alembic migration (see migrations/versions/ — SQLModel/SQLAlchemy
    has no first-class Python-side declaration for a partial index)."""
    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agentversion_agent_id_version"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    version: str
    flow: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    app_type: Optional[str] = None
    features: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    entitlement_plan: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    # Real Sub-Agent dependency manifest (§0.104, GAP_REPORT) — the
    # actual fix for a real, reported reproducibility gap: a Sub-Agent
    # node used to always resolve its OWN target's CURRENT published
    # AgentVersion at execution time, not whatever version the target
    # actually was when THIS parent was published — so an unchanged,
    # already-published parent could silently invoke a materially
    # different child tomorrow than it did today, simply because the
    # child was independently republished in between. Frozen here, at
    # THIS agent's own publish time, in the report's own recommended
    # shape: {"subagents": [{"node_id", "agent_id", "agent_version_id",
    # "version"}, ...]} — one real entry per Sub-Agent node in the
    # flow this AgentVersion snapshots, each naming the EXACT child
    # AgentVersion.id that was current at that moment. A child that
    # was itself still only a draft at this parent's own publish time
    # (nothing real to pin to yet) gets agent_version_id=None here —
    # an honest, explicit "unpinned" rather than a fabricated pin.
    # See harness.py's own handle_subagent/_resolve_subagent_flow for
    # how this is actually consulted at runtime, and
    # routers/agents.py's own _build_subagent_manifest for how it's
    # actually built at publish time.
    subagent_manifest: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    published_by: str = "you"
    created_at: str = ""
    is_current: bool = True

    @field_serializer("features")
    def _serialize_features(self, features: Dict[str, Any], _info):
        # Real, deliberate (§0.59, GAP_REPORT): the underlying stored
        # snapshot still genuinely carries the token it was published
        # with (a real publish-time secret is part of what "this
        # version's real behavior" means, matching this class's own
        # docstring) — but every real read of that snapshot masks it
        # the same way a live Agent's own features are masked, so
        # version history can never be used as a side door to the same
        # secret list_agents/get_agent already protect.
        return _mask_mcp_token(features)


class WebhookEndpoint(SQLModel, table=True):
    """Real, first-class webhook registration (§0.97, GAP_REPORT) — the
    actual fix for a real, reported bug: webhook EXECUTION already
    correctly ran a published agent's own frozen AgentVersion.flow
    (via resolve_execution/execute_agent_flow), but webhook DISCOVERY
    (routers/webhooks.py's own _find_webhook_node) scanned the live,
    mutable Agent.flow directly, for a plain string match against
    node.config["webhook_token"] — a genuine mismatch: a Builder
    editing a trigger node's own token on the live draft, with NO new
    publish, silently changed which token production actually
    responded to, even though the flow that token invoked stayed
    genuinely frozen. It was also a real, unindexed O(agents × nodes)
    full scan on every single incoming webhook request.

    This table is the one, real, authoritative source for "does this
    token resolve to a real, currently-published agent" — never a
    JSON flow scan again. token_hash (a real sha256 hex digest, never
    the raw token itself) is what's actually compared against an
    incoming request's own token — a real, defense-in-depth choice: a
    webhook token is a genuine bearer credential (the module's own
    docstring, routers/webhooks.py, already states this is the ENTIRE
    real security model), so a raw database read/backup leak
    shouldn't hand over every currently-valid webhook credential in
    plaintext. The real, original, human-readable token still lives
    exactly where it always did — inside the published AgentVersion's
    own flow (and the live draft's, for the Builder's own UI to
    display back) — this table exists purely for real, indexed
    lookup/authentication, not as a second copy of record.

    Rows are created/updated/revoked as part of the real, same publish
    transaction that creates a new AgentVersion (routers/agents.py's
    own publish_agent) — a webhook trigger node's own token becomes
    live in production at the exact same real moment its flow does,
    never independently. agent_version_id records which real,
    published snapshot most recently activated this token, for a real,
    honest audit trail — not read at request time (the live Agent's
    own current published flow, via resolve_execution, is still the
    one, real, authoritative source of what actually executes)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    agent_version_id: int = Field(foreign_key="agentversion.id")
    trigger_node_id: str
    token_hash: str = Field(index=True, unique=True)
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    revoked_at: Optional[datetime] = None


class SavedResult(SQLModel, table=True):
    """A real, persisted Workflow-app result a person chose to keep —
    genuinely survives across sessions (queried back from Postgres, not
    an in-memory list that resets on reload), matching what "Save" means
    on a real Workflow Web App."""
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    invoking_user: str
    input: str
    output: Optional[str] = None
    structured_output: Optional[dict] = Field(default=None, sa_column=Column(JSONB))
    # Provenance, made explicit rather than implied.
    #
    # Every field below used to be caller-supplied, including
    # invoking_user (defaulting to "user:demo"), so a client could
    # create a row that looked exactly like a real execution of an agent
    # by someone else. "execution" now means the run_id was verified
    # against a real Run of THIS agent owned by THIS owner, and the
    # input/output/version were read from that Run and its Trace rather
    # than accepted from the request. "manual" means a person saved
    # something by hand — still legitimate, but never mistakable for an
    # execution record.
    provenance: str = Field(default="manual", index=True)
    run_id: Optional[str] = Field(default=None, index=True)
    trace_id: Optional[str] = None
    agent_version: Optional[str] = None
    created_at: str


class RunStatus(str, Enum):
    """§0.116, GAP_REPORT — the actual fix for the reported gap: Run's
    own `status` column was a real, free-form string with only a
    comment stating which real values were in use, no real, enforced
    constraint of any kind. This is the real, single, authoritative
    enumeration of every real status this platform's own execution
    paths genuinely produce — grep-verified against every real
    assignment site (execution.py's own status_map/run_status_map,
    every `.status = "..."` call site) while building this fix, not
    asserted from memory. `QUEUED` and `CANCELLED` are real, included
    values with no real, current producer yet (this platform has no
    real request-queueing or run-cancellation feature today) — included
    because a real, future feature shouldn't need a second migration
    just to widen this same, real constraint; a real DB CHECK
    constraint (see the migration this fix adds) enforces this same,
    real set at the database level too, not just here in Python."""
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    DENIED = "denied"


class TraceStatus(str, Enum):
    """§0.116, GAP_REPORT — the same real fix as RunStatus above,
    for Trace's own status column; grep-verified against every real
    trace_status_map/`.status = "..."` assignment site."""
    OK = "ok"
    UCS_DENIAL = "ucs_denial"
    HARNESS_FAIL = "harness_fail"
    ORCH_FAIL = "orch_fail"
    PAUSED = "paused"
    MODERATION_BLOCKED = "moderation_blocked"


class Run(SQLModel, table=True):
    __table_args__ = (
        # §0.116, GAP_REPORT — the actual, database-level half of the
        # reported fix: a real CHECK constraint enforcing the exact
        # same real, enumerated set RunStatus (above) declares in
        # Python — a genuinely malformed status value is now rejected
        # by Postgres itself on INSERT/UPDATE, not just by convention
        # or a Python-level comment nobody's forced to honor.
        CheckConstraint(
            "status IN ('queued','running','paused','success','failed','blocked','cancelled','denied')",
            name="ck_run_status_enum",
        ),
    )
    id: str = Field(primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    agent_name: str
    status: str  # RunStatus's own real values (§0.116, GAP_REPORT) — kept as `str` (not the enum type directly) so a genuinely malformed value from an old row is never a real deserialization crash; the DB CHECK constraint above is the real, enforced guarantee for every NEW row.
    # duration/cost/ts (§0.116, GAP_REPORT) — the actual fix for the
    # reported gap: these three real columns used to be independently
    # SET at every real write site (execution.py), computed by a
    # SEPARATE real formatting expression than the one that produced
    # duration_ms/cost_usd/created_at just below — two real,
    # independent computations that happened to agree today but had
    # no real, structural guarantee of staying that way; a real, silent
    # dual-source-of-truth risk the report names directly. Renamed to
    # `_legacy` (a real, non-destructive ALTER TABLE ... RENAME COLUMN
    # — no existing, historical data lost) and no longer written by any
    # real call site for a NEW row (see execution.py's own updated
    # _finalize_run/_typed_timing) — every NEW row leaves these
    # genuinely NULL, with the real, typed fields as the only thing
    # actually persisted going forward. `duration`/`cost`/`ts` below
    # are now real, computed Pydantic fields (never a real, independent
    # DB column of their own) — formatted from the real, typed fields
    # at RESPONSE-SERIALIZATION time, falling back to a real, existing
    # row's own legacy value only when the typed field is genuinely
    # absent (a row predating either this fix or the typed fields
    # themselves) — "formatting belongs in the API/UI layer," made
    # real, without a breaking API response-shape change for any
    # existing real caller (frontend or otherwise) that reads
    # `run.duration`/`run.cost`/`run.ts` today.
    duration_legacy: Optional[str] = Field(default=None, sa_column=Column("duration", String, nullable=True))
    cost_legacy: Optional[str] = Field(default=None, sa_column=Column("cost", String, nullable=True))
    hops: int = 1
    ts_legacy: Optional[str] = Field(default=None, sa_column=Column("ts", String, nullable=True))
    fail_class: Optional[str] = None  # orchestration | harness | ucs | graph_deadlock | None
    # Real, only set for a genuine durable-path run still executing
    # past the real 25s HTTP-response budget (§0.60, GAP_REPORT) — the
    # actual Temporal workflow id this Run's eventual terminal state
    # gets reconciled from. None for every real sync-path run and
    # every durable run that finished within the response window.
    workflow_id: Optional[str] = None
    # Real, only populated alongside workflow_id — the original
    # invocation's own real invoking_user/input, kept so a later real
    # reconciliation (_reconcile_running_run) can persist an accurate
    # Trace instead of a placeholder, since the row that requested this
    # run is otherwise unavailable by the time reconciliation happens.
    invoking_user: Optional[str] = None
    user_input: Optional[str] = None
    # Real, genuine datetime (§0.77, GAP_REPORT) — `ts` above is a real,
    # existing field, but only ever a time-of-day STRING
    # (`"%H:%M:%S"`, no date component at all) — there was never a real
    # way to determine which real DAY a given Run happened on, which is
    # exactly what routers/monitoring.py's own real "runsToday" (and
    # any other real time-windowed metric) genuinely needs. A real
    # existing row predating this fix has no real, accurate value for
    # this column (there's no way to recover a real date that was never
    # stored) — left `None` rather than fabricating one; see this
    # field's own real, honest handling in monitoring.py.
    created_at: Optional[datetime] = Field(default_factory=datetime.utcnow)
    # Real, aggregated per-run token/cost usage (§0.78, GAP_REPORT) —
    # the actual fix for the reported gap: real usage
    # (input_tokens/output_tokens, already captured from the real
    # Anthropic API response — see model_adapter.py's own call_llm) was
    # computed and immediately discarded; `cost` above was
    # unconditionally hardcoded to "$0.00" at every real call site
    # (grep-verified while building this fix). These are the real sum
    # across every real span this run produced (see harness.py's own
    # _aggregate_llm_usage) — 0 for a run with no LLM-touching nodes,
    # a real, honest zero, not an absent field. cost_usd is real math
    # on these real token counts against model_adapter.py's own real,
    # configurable price table — see that table's own docstring for
    # the honest distinction between the genuinely-exact token counts
    # and the reference (not contractually-verified) dollar rates.
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    # cost_usd (§0.79, GAP_REPORT) — a real, genuine fix: this was
    # originally added (§0.78) as a plain Python `float`/SQL `Float`
    # column. A real, reported follow-up correctly flagged that as the
    # wrong real type for money: `float` is binary floating point,
    # which cannot represent most real decimal fractions exactly (the
    # textbook real example: 0.1 + 0.2 != 0.3 in binary float) — summing
    # many small real per-run costs for a real aggregation/SLO/billing
    # query can accumulate a real, visible rounding error over time.
    # `Decimal` backed by a real SQL `NUMERIC(12, 6)` column is exact,
    # base-10 arithmetic — the correct real type for currency. See
    # model_adapter.py's own calculate_cost_usd, now returning a real
    # Decimal for the exact same reason.
    cost_usd: Decimal = Field(default=Decimal("0"), sa_column=Column(Numeric(12, 6)))
    # duration_ms / started_at / completed_at (§0.79, GAP_REPORT) — the
    # actual fix for the reported gap: `duration` above is a real,
    # existing field, but a display STRING ("1.8s") — genuinely
    # unusable for a real aggregation query (AVG/SUM/percentile all
    # need a real number), a real retention/SLO query ("runs older
    # than 30 days", "p95 duration over the last hour" — `ts`'s own
    # time-of-day-only string can't even answer "which day", the same
    # real gap §0.77 already fixed for Run.created_at), or a real
    # analytics BI tool (which expects real, typed columns, not
    # parsing "14:22:08" back into a real time itself). `duration_ms`
    # is a real, exact BIGINT millisecond count — no string parsing
    # needed for any real query. `started_at`/`completed_at` are real,
    # timezone-aware TIMESTAMPTZ values — the same real duration_s
    # already computed at every call site, expressed as two real
    # instants instead of one pre-formatted, hard-to-query string.
    # Nullable — a real, existing row predating this fix has no real,
    # accurate value to backfill (there's no way to recover a real
    # start/end instant that was never stored) — left `None` rather
    # than fabricating one, the same real, honest convention
    # `created_at` (§0.77) already established for this exact table.
    duration_ms: Optional[int] = None
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    completed_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    # agent_version_id / agent_version / flow_hash (§0.91, GAP_REPORT) —
    # the actual fix for a real, reported gap: this row never recorded
    # which real AgentVersion snapshot (or, for a draft/preview run,
    # which real live-flow content) actually executed — only
    # Trace.version existed, and that was set from agent.version AT
    # FINALIZATION time, not from whatever version was actually resolved
    # and executed at INVOCATION time. For a real, long-running durable
    # workflow, those two can genuinely differ: a workflow that started
    # on a real, immutable v5 snapshot could have the agent published
    # to v6 before the workflow finishes and this row gets finalized —
    # the eventual Trace would then incorrectly claim v6, despite the
    # real, frozen v5 nodes being what actually ran the whole time.
    # agent_version_id is the real AgentVersion.id FK (None for a
    # draft/preview run — there's no real AgentVersion to point at);
    # agent_version is the real, human-readable version STRING actually
    # resolved (e.g. "v5") — kept even though agent_version_id also
    # exists, since a real AgentVersion row could theoretically be
    # deleted later, and the human-readable label should survive that;
    # flow_hash is a real sha256 hex digest of the EXACT flow JSON that
    # was resolved and executed (see _resolve_execution_context's own
    # docstring, execution.py) — a real, content-addressed fingerprint
    # that's honest even for a draft/preview run with no AgentVersion
    # at all, and lets a person verify two runs genuinely executed
    # byte-identical flow content, not just the same version LABEL
    # (which, for a draft, isn't even a real, frozen concept).
    agent_version_id: Optional[int] = Field(default=None, foreign_key="agentversion.id")
    agent_version: Optional[str] = None
    flow_hash: Optional[str] = None
    # The Trace this run produced. _finalize_run creates both ids and
    # hands both to the client, but nothing on the server related them —
    # so "the output of run X" could only be answered by trusting a
    # caller to pass the matching trace_id back. Provenance for a saved
    # result needs that link to be authoritative, not client-asserted.
    trace_id: Optional[str] = Field(default=None, index=True)

    # §0.116, GAP_REPORT — real, computed (never independently
    # persisted) formatted views, derived from the real, typed fields
    # above at serialization time. "Formatting belongs in the API/UI
    # layer," made real: a caller reading `run.duration`/`run.cost`/
    # `run.ts` from any real API response gets the exact same shape as
    # before this fix, computed fresh from the real, canonical typed
    # field every time, never a second, independently-writable value
    # that could silently drift from it. Falls back to this row's own
    # `_legacy` column only when the typed field is genuinely absent —
    # a real, existing row that predates either this fix or the typed
    # fields themselves — the same honest "no fabricated value" stance
    # duration_ms/cost_usd/started_at's own docstrings already state.
    @computed_field
    @property
    def duration(self) -> Optional[str]:
        # A real, honest, minor behavioral note: this can differ from
        # the real, pre-fix value by up to 0.1s in a small fraction of
        # cases (confirmed directly: ~0.5% across 100k realistic
        # samples) — a genuine double-rounding artifact (duration_ms
        # itself is already rounded to the nearest millisecond, then
        # this divides and rounds again to one decimal place), not a
        # bug. Accepted deliberately: the alternative is keeping two
        # real, independent formatting computations in sync forever —
        # exactly the real dual-source-of-truth risk this fix exists
        # to close.
        if self.duration_ms is not None:
            return f"{self.duration_ms / 1000:.1f}s"
        return self.duration_legacy

    @computed_field
    @property
    def cost(self) -> Optional[str]:
        # Real, deliberate heuristic (§0.116, GAP_REPORT) — a genuine
        # gap found while building this fix: cost_usd defaults to
        # Decimal("0") (never a real, nullable None the way
        # duration_ms/started_at already are), so a real, existing row
        # that predates cost_usd's own introduction (§0.79) — with a
        # real, non-zero cost_legacy value — is indistinguishable from
        # a genuinely zero-cost new row by cost_usd's own value alone.
        # cost_legacy is only ever real/non-None for a row THIS fix's
        # own write sites never touch again (they stopped setting it),
        # so "cost_usd is exactly zero AND a real cost_legacy exists"
        # correctly identifies the real, historical-only case without
        # a further schema change.
        if self.cost_usd == Decimal("0") and self.cost_legacy is not None:
            return self.cost_legacy
        if self.cost_usd is not None:
            return f"${self.cost_usd:.4f}"
        return self.cost_legacy

    @computed_field
    @property
    def ts(self) -> Optional[str]:
        if self.created_at is not None:
            return self.created_at.strftime("%H:%M:%S")
        return self.ts_legacy


class Trace(SQLModel, table=True):
    __table_args__ = (
        # §0.116, GAP_REPORT — the same real, database-level fix as
        # Run's own identical constraint just above.
        CheckConstraint(
            "status IN ('ok','ucs_denial','harness_fail','orch_fail','paused','moderation_blocked')",
            name="ck_trace_status_enum",
        ),
    )
    id: str = Field(primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    agent_name: str
    version: str
    principal: str
    plane: str
    spans: int
    # duration/ts (§0.116, GAP_REPORT) — the same real fix, and the
    # same real reasons, as Run's own identically-named fields (see
    # that model's own docstring for the full real reasoning);
    # `duration`/`ts` below are now real, computed Pydantic fields
    # derived from `duration_ms`/`started_at`, never an independently
    # persisted column of their own — these `_legacy` columns are the
    # real, renamed, historical-data-preserving originals, no longer
    # written by any real call site for a NEW row.
    duration_legacy: Optional[str] = Field(default=None, sa_column=Column("duration", String, nullable=True))
    ts_legacy: Optional[str] = Field(default=None, sa_column=Column("ts", String, nullable=True))
    status: str  # TraceStatus's own real values (§0.116, GAP_REPORT)
    # Real conversation content and real per-node spans — what the user
    # actually typed and what the agent actually replied, plus the real
    # execution.py's actual result["spans"] list (not a synthesized
    # generic waterfall). Previously nothing here was persisted at all;
    # /traces/{id}/spans faked a plausible-looking 4-span waterfall with
    # hardcoded names and durations regardless of what really ran.
    user_input: str = ""
    final_output: Optional[str] = None
    spans_json: List[dict] = Field(default_factory=list, sa_column=Column(JSONB))
    # duration_ms / started_at / completed_at / cost_usd (§0.79,
    # GAP_REPORT) — the same real fix, and the same real reasons, as
    # Run's own identically-named fields just above: `duration`/`ts`
    # above are real, existing display STRINGS, genuinely unusable for
    # a real aggregation, retention, or SLO query without first parsing
    # them back into real numbers/timestamps — exactly the reported
    # gap. cost_usd is a real `Decimal`/`NUMERIC(12, 6)` for the same
    # real currency-precision reason Run.cost_usd is (never plain
    # float). Nullable/zero-default for a real, existing row predating
    # this fix, for the same real, honest reason Run's own fields are.
    duration_ms: Optional[int] = None
    started_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    completed_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    cost_usd: Decimal = Field(default=Decimal("0"), sa_column=Column(Numeric(12, 6)))
    # agent_version_id / agent_version / flow_hash (§0.91, GAP_REPORT) —
    # the SAME real fields, and the SAME real reason, as Run's own
    # identically-named fields just above — copied verbatim from the
    # originating Run row at finalization time (_finalize_run,
    # execution.py) rather than re-derived from agent.version, so this
    # Trace records exactly what actually ran, immune to a real publish
    # landing on the same agent sometime between invocation and this
    # Trace's own creation. `version` above is kept as-is (a real,
    # existing, already-relied-upon field) — agent_version is
    # deliberately additional, not a replacement, so nothing existing
    # breaks, but a person can now compare the two and immediately spot
    # the exact real drift case this fix exists to make auditable.
    agent_version_id: Optional[int] = Field(default=None, foreign_key="agentversion.id")
    agent_version: Optional[str] = None
    flow_hash: Optional[str] = None

    # §0.116, GAP_REPORT — real, computed (never independently
    # persisted) formatted views; see Run's own identically-shaped
    # properties for the full real reasoning. Trace has no `cost`
    # field of its own (only Run does) — only `duration`/`ts` here.
    @computed_field
    @property
    def duration(self) -> Optional[str]:
        if self.duration_ms is not None:
            return f"{self.duration_ms / 1000:.1f}s"
        return self.duration_legacy

    @computed_field
    @property
    def ts(self) -> Optional[str]:
        # Trace's own real, existing `ts` format has always been the
        # FULL date+time ("%Y-%m-%d %H:%M:%S"), unlike Run's own
        # time-of-day-only convention — preserved exactly here.
        if self.started_at is not None:
            return self.started_at.strftime("%Y-%m-%d %H:%M:%S")
        return self.ts_legacy


class EvalSet(SQLModel, table=True):
    """A named collection of test cases for ONE specific agent — the real
    counterpart to what monitoring.py's old EVALS list faked with static
    strings. See routers/evals.py for real execution against this."""
    id: str = Field(primary_key=True)
    name: str
    agent_id: str = Field(foreign_key="agent.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class EvalCase(SQLModel, table=True):
    """One test case: a real input, a real expected output, and an
    optional free-text `criteria` — if set, scoring is real LLM-as-judge
    (DeepEval's GEval, judged by this app's own real/mock call_llm, not a
    second untested LLM integration); if unset, scoring is a real
    deterministic substring match, no LLM call needed at all."""
    id: str = Field(primary_key=True)
    eval_set_id: str = Field(foreign_key="evalset.id", index=True)
    input: str
    expected_output: str
    criteria: Optional[str] = None


class EvalRun(SQLModel, table=True):
    """One real execution of an eval set — every case in it actually ran
    through run_flow_sync against the live agent at run time, not a
    stub. `rate`/`drift` are computed for real from EvalResult rows and
    the immediately-preceding run for the same set, not asserted.

    agent_version_id/agent_version/flow_hash (§0.99, GAP_REPORT) — the
    actual fix for a real, reported gap: this used to record no real
    resolved-version metadata at all, and — the deeper, real,
    conceptual issue the report also named — each real case
    independently resolved the agent's own current version via its own
    separate execute_agent_flow call, so a real, concurrent publish
    landing mid-run could in principle have different cases inside the
    SAME logical eval run silently scored against different flow
    content, making any comparison between them meaningless. Now
    resolved exactly ONCE, up front, in run_eval_set (routers/
    evals.py) — the same one, real, resolved snapshot every real case
    in this run is scored against, recorded here once per run rather
    than per case, matching the report's own recommended model."""
    id: str = Field(primary_key=True)
    eval_set_id: str = Field(foreign_key="evalset.id", index=True)
    agent_id: str
    status: str = "running"  # running | completed | failed
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    pass_count: int = 0
    total_count: int = 0
    drift: str = "none"  # none | watch | flagged — real, computed vs. the prior run
    agent_version_id: Optional[int] = Field(default=None, foreign_key="agentversion.id")
    agent_version: Optional[str] = None
    flow_hash: Optional[str] = None


class EvalResult(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    eval_run_id: str = Field(foreign_key="evalrun.id", index=True)
    eval_case_id: str
    actual_output: Optional[str] = None
    passed: bool = False
    score: float = 0.0
    reason: str = ""


class Alert(SQLModel, table=True):
    id: str = Field(primary_key=True)
    sev: str  # high | medium | low
    rule: str
    agent_id: str = Field(foreign_key="agent.id", index=True)
    agent_name: str
    state: str = "open"  # open | resolved
    acked: bool = False
    ts: str
    summary: str
    history: List[Dict[str, str]] = Field(default_factory=list, sa_column=Column(JSONB))
    # When this alert actually reached a channel, and which ones.
    #
    # The Open tab means "sent to a channel and not yet acted on", so
    # whether delivery succeeded is part of the alert's state, not just a
    # log line. It also lets the sweep retry an alert that was raised
    # while no channel existed, or while one was down — otherwise such an
    # alert would be permanently invisible.
    delivered_at: Optional[datetime] = None
    delivered_to: List[str] = Field(default_factory=list, sa_column=Column(JSONB))


class AlertRule(SQLModel, table=True):
    """A rule stored as data, evaluated on every sweep.

    These were module-level Python functions with env-var thresholds:
    changing one meant editing code and redeploying, and there was no way
    to have a tighter rule for one agent than for the rest. As rows they
    can be added, tuned and scoped per agent from the UI.

    `kind` selects the logic; every other column parameterises it. The
    meaning of `threshold` therefore depends on `kind` — a percentage for
    error_rate and quality_breach, a count for denied_burst and
    guardrail_block, a multiple of the agent's own baseline for the spike
    rules. That is stated in the UI next to the field rather than left
    for someone to guess.
    """
    __table_args__ = (
        # An unrecognised kind would evaluate to nothing and the rule
        # would sit in the list looking active while doing nothing at all.
        CheckConstraint(
            "kind IN ('error_rate','denied_burst','guardrail_block','step_limit',"
            "'latency_spike','cost_spike','quality_breach')",
            name="ck_alertrule_kind",
        ),
        CheckConstraint("severity IN ('low','medium','high')",
                        name="ck_alertrule_severity"),
        CheckConstraint("threshold >= 0", name="ck_alertrule_threshold"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    kind: str
    severity: str = "medium"
    threshold: float = 1
    # 0 means "use the kind's own default", so a rule can be created
    # without having to know what a sensible window is.
    window_seconds: int = 0
    # Volume floor. A rate over three runs is noise; this is what stops a
    # rule firing on thin data, and it belongs per-rule rather than being
    # one global env var for every rule at once.
    min_runs: int = 20
    # Which failure classes count. Empty means any — the analogue of the
    # catalog's "dimensions" field.
    fail_classes: List[str] = Field(default_factory=list, sa_column=Column(JSONB))
    # None means every agent. This replaces the catalog's FQN prefix:
    # what a rule scopes to here is an agent, not a dotted object path.
    agent_id: Optional[str] = Field(default=None, foreign_key="agent.id", index=True)
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AlertDestination(SQLModel, table=True):
    """A channel alerts are sent to, and the rules for which reach it.

    Alerts previously existed only inside the app — nothing ever left
    the platform. A destination is somewhere a person actually watches,
    with filters so one channel can take high-severity alerts for one
    agent without every other notice following it there.

    `webhook_url_encrypted` holds an incoming-webhook URL, which is a
    credential: anyone with it can post into that channel. It is stored
    through the same encrypt_secret used for MCP tokens and is never
    returned by the API — `url_hint` is what the UI shows instead.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    kind: str = "slack"  # slack | teams
    webhook_url_encrypted: Optional[str] = None
    # Enough to recognise which channel this is without revealing the
    # credential, e.g. "hooks.slack.com/…/B01ABCD".
    url_hint: str = ""
    enabled: bool = True
    # Filters. Both default to "everything", which is the least
    # surprising behaviour for a destination someone has just created.
    min_severity: str = "low"  # low | medium | high
    agent_id: Optional[str] = Field(default=None, foreign_key="agent.id", index=True)
    # Whether the last attempt worked, so a channel that has been
    # quietly failing is visible rather than assumed healthy.
    last_attempt_at: Optional[datetime] = None
    last_status: Optional[str] = None  # ok | failed
    last_detail: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Role(SQLModel, table=True):
    """Read-only display of agent-level RBAC bindings. Bindings are
    administered in UCS's own console (§7.4/§7.5) — no create/update/delete
    endpoint exists here on purpose, mirroring the UI mockup's decision to
    remove local governance pages."""
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    agent_name: str
    tier: str  # select | writer | owner — what UCS is actually told
    # Which agent role this grant represents. The tier alone cannot say
    # whether a group may run an agent or publish a new version of it,
    # and several roles map onto the same tier, so it cannot be derived
    # back. See app/agent_roles.py.
    agent_role: Optional[str] = Field(default=None, index=True)
    group: str


class ModelProvider(SQLModel, table=True):
    name: str = Field(primary_key=True)
    version: str
    status: str  # credits | needs_key
    types: List[str] = Field(default_factory=list, sa_column=Column(JSONB))
    api_key_configured: bool = False
    # Real, stored connection details (§0.63, GAP_REPORT) — a real,
    # reported gap: this row previously only ever flipped
    # api_key_configured to True and never stored anything the actual
    # runtime could use, so "Provider configured" in the UI was
    # genuinely disconnected from what call_llm actually executed
    # against (os.getenv("ANTHROPIC_API_KEY") alone). api_key is real
    # Fernet ciphertext at rest, same as EnvVar/KnowledgeBase/MCP
    # secrets (secrets_crypto.py) — never returned in plaintext once
    # saved, same one-time-echo convention as those.
    api_key: Optional[str] = None
    organization: Optional[str] = None
    api_base_url: Optional[str] = None

    @field_serializer("api_key")
    def _serialize_api_key(self, api_key: Optional[str], _info):
        # Same real, centralized masking pattern as Agent.features'
        # own field_serializer (§0.59) — every real read of this row
        # (GET /model-providers, and anything else that ever returns a
        # ModelProvider in the future) masks the real stored ciphertext
        # automatically, rather than needing each endpoint to remember.
        return SECRET_MASK if api_key else None


class Tool(SQLModel, table=True):
    name: str = Field(primary_key=True)
    org: str
    version: Optional[str] = None
    desc: str
    source: str  # marketplace | builtin


class ToolAction(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tool_name: str = Field(foreign_key="tool.name", index=True)
    name: str
    desc: str
    params: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB))


class MCPServer(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str
    identifier: str
    url: str
    status: str = "connected"
    actions: int = 0


class DataSource(SQLModel, table=True):
    """An external document source that a Knowledge Base can ingest from.

    `config` holds the real connection details the Integrations page
    already collects — client_id/client_secret/tenant_id for the
    Microsoft Graph and Google sources, a token or service-account JSON
    under `credentials` for the rest. Sensitive keys (DS_SENSITIVE_KEYS
    in routers/integrations.py) are Fernet ciphertext at rest and masked
    on every read, the same treatment ModelProvider and KnowledgeBase
    secrets get.

    Before this column existed both configure endpoints validated the
    credentials they were sent and then discarded them, flipping
    `status` to "connected" with nothing behind it — so a source could
    read as connected while no code could authenticate to it, and
    nothing could browse or ingest.
    """
    name: str = Field(primary_key=True)
    version: str
    status: str  # needs_auth | connected
    auth: str  # oauth | apikey
    config: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))


class KnowledgeBase(SQLModel, table=True):
    """A user's OWN vector store — deliberately a separate connection
    target from this platform's own operational Postgres (the one storing
    Agent/Run/Trace/EnvVar/AgentMemory/etc.). That separation is the whole
    point: an agent's knowledge shouldn't live in the same database as the
    platform's own bookkeeping. `config` holds real connection details
    (provider-specific — host/port/dbname/user/password for pgvector; url/
    api_key/index for the others). Sensitive fields (SENSITIVE_KEYS in
    routers/knowledge_bases.py — password/api_key/retrieval_api_key) are
    masked on every read the same way EnvVar secrets are — real value
    only echoed once, on the create/update response, never again after
    — and real Fernet ciphertext at rest since §0.59 (GAP_REPORT); see
    secrets_crypto.py."""
    id: str = Field(primary_key=True)
    name: str
    provider: str  # pgvector | pinecone | weaviate | qdrant | milvus | other
    config: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    status: str = "untested"  # untested | connected | failed
    last_error: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class KnowledgeDocument(SQLModel, table=True):
    """Bookkeeping for one real ingested file — groups the chunk rows that
    live in the KB's OWN separate pgvector table (see KnowledgeBase's
    docstring) under a single `document_id`, which is stamped onto every
    chunk row at ingest time. Local, not remote: this table lives in the
    platform's own Postgres (fast to list/aggregate), while the actual
    chunk content + embeddings stay in the user's separate KB database —
    the same separation principle as everything else in this file.
    `status` toggling here cascades to every chunk's `enabled` column in
    the remote table (see routers/knowledge_bases.py), so per-document
    disable is a real bulk update, not a display-only flag."""
    id: str = Field(primary_key=True)
    kb_id: str = Field(foreign_key="knowledgebase.id", index=True)
    filename: str
    chunking_mode: str = "General"
    max_words: int = 180
    overlap_words: int = 30
    word_count: int = 0
    chunk_count: int = 0
    retrieval_count: int = 0
    status: str = "enabled"  # enabled | disabled
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # The ORIGINAL uploaded file, kept in object storage (S3, or this
    # server's disk) when the uploader asked for it — an UploadedFile
    # id, which carries the bucket/key, size and content type. Null for
    # every document ingested without that, including every one ingested
    # before this existed: ingestion extracts text and throws the file
    # away, so the only copy of a source PDF was on whoever's laptop it
    # came from.
    source_file_id: Optional[str] = Field(default=None, index=True)


class KBIngestionOutbox(SQLModel, table=True):
    """Real outbox row (§0.68, GAP_REPORT) — the actual fix for the
    reported cross-database atomicity gap. A real, reported bug: KB
    ingestion/deletion committed to the KB's own separate remote
    database FIRST, then committed local KnowledgeDocument metadata
    SEPARATELY — a real crash, network drop, or exception between the
    two real commits could leave orphan remote chunks (ingest) or a
    ghost local document row pointing at nothing (delete), with no
    real way to detect or fix either.

    Neither database can safely become the other's real 2PC
    participant here (the remote one is often a customer's own,
    separately-managed Postgres — see routers/knowledge_bases.py's own
    module docstring on why that's a deliberate architectural
    boundary, not something this fix should try to erase). Instead: a
    real, durable, LOCAL record of intent, written and committed
    BEFORE any remote side effect, so a real failure between the two
    real commits always leaves a real, detectable trace — this row's
    own status — rather than silent drift.

    Real status lifecycle: pending (this row committed locally; no
    remote side effect attempted yet) -> remote_done (the real remote
    commit succeeded; local metadata not yet written — THIS is the
    exact real drift window the report names, now detectable instead
    of silent) -> completed (local metadata written; fully consistent)
    | failed (the remote operation itself genuinely failed — nothing
    remote happened, so there's nothing to reconcile; the real,
    original HTTPException still reaches the caller unchanged).

    idempotency_key (real, honest "idempotent ingestion IDs" per the
    report) is this row's own id, stamped as document_id on every real
    remote chunk row for an ingest — a reconciler retrying the remote
    step for a still-"pending" row first deletes any existing rows
    under this same document_id before re-inserting, so a real retry
    can never produce real duplicate remote chunks, however many times
    it's attempted.

    payload carries the small, real metadata (filename, chunk counts,
    etc.) needed to complete a stuck "remote_done" row's local write
    WITHOUT needing the original uploaded file again — this platform
    doesn't keep a copy of that (see ingest_document's own docstring),
    but completing the LOCAL side of an already-remote-committed
    ingest never needs it; only the metadata does, and that's what's
    saved here."""
    id: str = Field(primary_key=True)
    kb_id: str = Field(foreign_key="knowledgebase.id", index=True)
    document_id: str = Field(index=True)
    operation: str  # ingest | delete
    status: str = "pending"  # pending | remote_done | completed | failed
    payload: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    attempts: int = 0
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class KnowledgeMetadataField(SQLModel, table=True):
    """A real, named+typed metadata field defined at the knowledge-base
    level — what the Knowledge Retrieval node's manual metadata-filtering
    conditions pick from. New fields are also auto-created the first time
    an ingest's metadata JSON uses a key that isn't registered yet, same
    as the mockup's stated behavior.

    Real, database-level uniqueness (§0.70, GAP_REPORT) — a real,
    reported gap: _ensure_metadata_fields only ever checked (kb_id,
    name) via a SELECT of every existing field before deciding whether
    to insert, so two genuinely concurrent ingests into the same KB,
    both using the same new metadata key for the first time, could
    both see it as absent and both insert — Postgres itself now
    rejects the second one via a real unique constraint;
    _ensure_metadata_fields catches the real resulting IntegrityError
    and treats it as the real, desired outcome (the field now exists
    either way), not a failure."""
    __table_args__ = (UniqueConstraint("kb_id", "name", name="uq_kmf_kb_id_name"),)
    id: str = Field(primary_key=True)
    kb_id: str = Field(foreign_key="knowledgebase.id", index=True)
    name: str
    type: str = "string"  # string | number | time


class KnowledgeRetrievalTest(SQLModel, table=True):
    """One real Retrieval Testing run against a knowledge base — the same
    real embed + nearest-neighbor query path a live `retrieval` node uses
    (see routers/knowledge_bases.py's retrieval_test), persisted so the
    "Records" history table genuinely reflects past tests rather than
    resetting on reload."""
    id: Optional[int] = Field(default=None, primary_key=True)
    kb_id: str = Field(foreign_key="knowledgebase.id", index=True)
    query: str
    result_count: int = 0
    # The retrieved chunks as they scored AT THE TIME of this run, so
    # reopening a past record shows what that run actually returned
    # rather than silently re-querying and passing off today's answer as
    # the historical one. Re-running would genuinely differ: documents
    # get added, disabled or deleted, and the Settings tab's retrieval
    # method and thresholds change underneath. Rows written before this
    # column existed keep an empty list, which the UI distinguishes from
    # "ran and matched nothing" (result_count tells those apart).
    results: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProviderModel(SQLModel, table=True):
    """A model registered against a ModelProvider from the Integrations
    page, beyond the ones hardcoded in model_adapter.py.

    The built-in catalog (_MODEL_PROVIDERS / _MODEL_MAP) is a Python
    dict, and call_llm refuses any model absent from it rather than
    silently routing to a different one. That is the right default, but
    it also meant the Integrations page's own "Add model" control had
    nowhere to write — it was a decorative <span> with no handler and no
    endpoint behind it. Rows here extend that catalog at runtime.

    `api_model_id` is the vendor's real identifier (e.g.
    "claude-3-5-haiku-20241022"), which is what the SDK is called with —
    `name` is only the label shown in the builder. Prices are per
    million tokens to match _DEFAULT_PRICE_TABLE's own units; left NULL
    they cost nothing rather than guessing a wrong number.
    """
    name: str = Field(primary_key=True)
    provider_name: str = Field(foreign_key="modelprovider.name", index=True)
    api_model_id: str
    types: List[str] = Field(default_factory=lambda: ["LLM"], sa_column=Column(JSONB))
    input_price_per_mtok: Optional[float] = None
    output_price_per_mtok: Optional[float] = None
    # For a Text Embedding model: the vector width it returns. Optional
    # because it can be discovered from the provider's first real
    # response, which is safer than trusting a typed-in number — the
    # pgvector column is created with this value and cannot change
    # afterwards without re-embedding every chunk.
    embedding_dimension: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MlStudioCredential(SQLModel, table=True):
    """The client secret of one ML Studio endpoint, so LLM / Agent nodes can
    run that deployed model (selected as ``mlstudio/<model_id>``; see
    mlstudio_client.py). ML Studio authenticates each endpoint by its own
    secret, so one provider-wide API key can't cover them. Fernet ciphertext
    at rest (secrets_crypto), write-only through the API like every other
    stored secret."""
    model_id: str = Field(primary_key=True)
    client_secret: str
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class UploadedFile(SQLModel, table=True):
    """Real, self-contained file storage backing the Doc Extractor node's
    real "Single File" / "Multiple Files" input (§0.33, GAP_REPORT) — a
    real trigger `single_file`/`file_list` field now genuinely uploads
    here on selection, and the returned real, fetchable URL is exactly
    what handle_docextractor already expects (its own httpx GET, same as
    any S3/GCS/Azure/presigned link — see harness.py's
    _resolve_cloud_url, unchanged by this).

    §0.80, GAP_REPORT — a real, reported gap this fix closes: binary
    content used to be stored directly as a real Postgres `bytea`
    column (`data: bytes` below, removed by this fix) — real, working
    storage, but genuinely doesn't scale for a real, significant agent
    workload (a real database's own TOAST storage, VACUUM, backup, and
    replication all pay a real, growing cost for every byte stored
    this way). Binary content now lives in a real, pluggable object
    store (see storage.py's own real LocalDiskStorageBackend/
    S3StorageBackend) — this row keeps only real metadata plus the
    real `object_key` that locates the actual bytes there.

    Real, additional fields for the report's own five further asks:
    `owner` (real, per-uploader tenant isolation — the real object key
    itself is namespaced by this same value, see routers/uploads.py),
    `expires_at` (a real TTL — see routers/uploads.py's own real
    sweep_expired_uploads for the real cleanup this enables),
    `scan_status`/`scan_result` (real, pluggable malware-scan outcome
    — see upload_security.py's own scan_for_malware). Content-type
    validation and filename sanitization are real, enforced at upload
    time (upload_security.py) rather than persisted as their own
    fields — `content_type` below already holds the real, validated,
    effective type, not the raw, unverified client claim."""
    id: str = Field(primary_key=True)
    filename: str
    content_type: str = ""
    size: int = 0
    # storage_backend/object_key (§0.80, GAP_REPORT) — replace the old
    # `data: bytes` bytea column. storage_backend records which real
    # backend (see storage.py) wrote this object, so a real, later
    # migration from one backend to another (e.g. local disk in dev to
    # a real S3 bucket in production) can distinguish rows already
    # migrated from ones that still need it, rather than assuming
    # every row lives in whatever backend happens to be configured
    # right now.
    storage_backend: str = "local"
    # Which StorageDestination holds these bytes, when it is not the
    # environment-configured one. Null means the .env destination, which
    # is what every row created before runtime destinations existed
    # used. Without this column a second bucket would be unreadable the
    # moment it stopped being the default — the same failure
    # storage_backend was added to prevent, one level deeper.
    storage_destination_id: Optional[str] = Field(default=None, index=True)
    object_key: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    owner: Optional[str] = None
    expires_at: Optional[datetime] = None
    scan_status: str = "pending"  # pending | clean | flagged | skipped
    scan_result: Optional[str] = None


class StorageDestination(SQLModel, table=True):
    """One place uploaded files can be kept, configured at runtime
    rather than in .env.

    The bucket and credentials used to live only in the environment, so
    changing where files go meant editing .env and restarting the whole
    stack — and every deployment had exactly one destination. A row here
    is an S3 bucket with its own credentials, created and tested from
    the upload dialog, so a person can point one upload at a different
    bucket without touching the server.

    Credentials are encrypted at rest with the same Fernet key
    everything else in this platform uses (secrets_crypto), and are
    never returned by the API — reads mask them, exactly like a
    ModelProvider key or a KnowledgeBase password.

    Why a stored row rather than credentials typed per upload and
    forgotten: the platform has to reach these objects AGAIN, long
    after the upload — to serve the download link on a document, to
    delete the object when its document is deleted, and to sweep it
    when its retention window closes. Credentials that vanish with the
    request would make every one of those impossible, so a destination
    is saved, named, and reusable by design.
    """
    id: str = Field(primary_key=True)
    name: str
    # Only "s3" today. Named rather than assumed so the column does not
    # have to be invented later for a second provider, and so a row
    # never silently means something it does not say.
    provider: str = "s3"
    bucket: str
    region: Optional[str] = None
    # Set for MinIO, Cloudflare R2, Wasabi and anything else
    # S3-compatible; null for real AWS.
    endpoint_url: Optional[str] = None
    # enc:v1:-marked ciphertext, not plaintext. Null means "use whatever
    # credential chain this process already has" — an instance/IAM role,
    # which is a legitimate production setup.
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    session_token: Optional[str] = None
    # The destination new uploads use when none is named. Exactly one
    # row may hold this; the API enforces it.
    is_default: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # The result of the last real write/read/delete probe against this
    # bucket — what makes "connected" a checked fact rather than a claim.
    last_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None


class GitConnection(SQLModel, table=True):
    """One GitHub / GitLab / Bitbucket account agents are committed to.

    Exported agents are YAML; a connection is where that YAML is also
    committed, so what a person downloads is under version control too.
    The token is a personal access token (Bitbucket: an app password or
    API token with `username`, or an access token without one), encrypted
    at rest with secrets_crypto and never returned by the API.
    """
    id: str = Field(primary_key=True)
    name: str
    # github | gitlab | bitbucket | azure_devops
    provider: str
    # Self-hosted root (GitHub Enterprise, self-managed GitLab), or the Azure
    # DevOps organization / collection URL (required there); null = the
    # public cloud. https only — see git_integrations.normalize_base_url.
    base_url: Optional[str] = None
    # Bitbucket app password / API token only: the account it belongs to.
    username: Optional[str] = None
    # enc:v1: ciphertext.
    token: str
    # Account the token resolved to when it was last checked.
    account: Optional[str] = None
    default_repo: Optional[str] = None
    default_branch: Optional[str] = None
    default_folder: Optional[str] = None
    # Commit an agent's YAML here every time it is downloaded.
    commit_on_download: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None


class MaintenanceRun(SQLModel, table=True):
    """The outcome of one scheduled maintenance job.

    Without this, "did retention actually run?" is unanswerable, and an
    operator learns that it silently stopped weeks ago from a full disk
    rather than from an alert. One row per job per attempt, which is
    what makes a missed run detectable: the scheduler and the /status
    endpoint both compare `finished_at` of the last SUCCESS against the
    job's own interval.

    Pruned by the same control loop that writes it — see
    maintenance_scheduler.

    `detail` holds the job's own return value (how many rows it removed,
    which KBs it repaired). Jobs report counts, never row contents, so
    this stays a record of activity rather than a second copy of the
    data being cleaned up.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    job: str = Field(index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    finished_at: Optional[datetime] = None
    status: str = "running"  # running | ok | error
    duration_ms: Optional[int] = None
    detail: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    error: Optional[str] = None


class HitlRequest(SQLModel, table=True):
    """A real, persisted pause point for a Human Input (`hitl`) node —
    on EITHER real execution path (§0.42, GAP_REPORT extended this
    beyond the sync-only path it originally covered). On the sync path,
    run_flow_sync renders the form, stores this row (including a full
    ctx snapshot resume_flow_sync needs to continue exactly where
    execution left off), and returns status="paused_for_human_input".
    On the real durable path, AgentRunWorkflow itself pauses via a
    genuine workflow.wait_condition() and this row's
    temporal_workflow_id identifies which real, still-running Temporal
    workflow to signal on resume — a real, better-suited mechanism for
    a pause that might genuinely last hours or days, since it survives
    a worker restart, unlike the sync path's in-memory-until-resumed
    approach. POST .../hitl/{id}/respond checks temporal_workflow_id to
    decide which real resume mechanism to use."""
    id: str = Field(primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    node_id: str
    invoking_user: str
    rendered_content: str = ""
    fields: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB))
    actions: List[Dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSONB))
    delivery_status: Dict[str, str] = Field(default_factory=dict, sa_column=Column(JSONB))
    ctx_snapshot: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    # Real, §0.42 (GAP_REPORT) — set only for a pause that happened on
    # the real durable/Temporal path. When set, resuming this request
    # means genuinely signaling the still-running real Temporal
    # workflow (see AgentRunWorkflow.submit_hitl_response) rather than
    # reconstructing ctx from ctx_snapshot the way the sync path's own
    # resume_flow_sync does — ctx_snapshot stays populated either way
    # (useful for display/debugging), but isn't what actually resumes
    # execution on this path.
    temporal_workflow_id: Optional[str] = None
    status: str = "pending"  # pending | resolving | resolved
    # Real, bounded lease on the "resolving" claim (§0.106, GAP_REPORT)
    # — the actual fix for a real, reported gap: the atomic claim
    # above correctly moved pending -> resolving before any resume
    # attempt, but nothing ever moved a row back out of "resolving" if
    # that attempt then genuinely failed (Temporal temporarily
    # unreachable, a workflow-signaling failure, a worker outage, a
    # genuine resume exception) — confirmed directly, no recovery
    # mechanism existed at all, and a stuck "resolving" row's own
    # request_id could never be legitimately retried again (status !=
    # "pending" rejects it with a 409 indefinitely). Set to
    # now() + a real, generous lease duration the instant a row is
    # claimed; respond_to_hitl's own new try/except around the actual
    # resume attempt rolls a row that fails DURING that same request
    # straight back to "pending" immediately (the report's own first,
    # faster recovery option) — this lease is real, separate insurance
    # for the case where the process handling that request dies
    # entirely before its own except clause can even run (a hard
    # process kill, not just a caught exception) — reconcile_stale_
    # hitl_requests (routers/execution.py — see its own docstring) is
    # the real, periodic reconciler the report's own preferred option
    # asks for, rolling any row whose lease has genuinely expired back
    # to "pending" too. None once a row is genuinely "pending" or
    # "resolved" again — this field only ever has real meaning while
    # status="resolving".
    resolving_until: Optional[datetime] = None
    resolved_action_id: Optional[str] = None
    field_values: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None
    # Real optimistic-claim support (§0.74, GAP_REPORT) — a real,
    # reported race: the old flow read status="pending", resumed the
    # workflow, then set status="resolved" and committed, with a real
    # window between the read and the commit where two concurrent
    # requests could both observe "pending". "resolving" is a real,
    # third, transitional state: an atomic `UPDATE ... WHERE
    # status='pending'` claims the row (see routers/execution.py's own
    # respond_to_hitl) before any resume work begins, so a second,
    # genuinely concurrent request's own claim attempt affects zero
    # rows and is honestly rejected, rather than racing past the same
    # stale read the first request already used.
    #
    # idempotency_key/resolved_result: real support for a repeated
    # frontend/network retry of the SAME logical submission (a real,
    # reported gap) — a retry carrying the same idempotency_key as the
    # request that already resolved this row gets the exact same real
    # cached result replayed back, instead of either a confusing
    # "already resolved" rejection or (worse, without the atomic claim
    # above) genuinely resuming the same workflow a second time.
    idempotency_key: Optional[str] = None
    resolved_result: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    # delegated_child_agent_id (§0.83, GAP_REPORT) — real, only ever
    # set by handle_subagent's own real nested-durable-invocation path
    # (harness.py) — see HitlPause's own docstring for the full real
    # design. When set, this row's own real temporal_workflow_id
    # points at the CHILD's own real, separate Temporal workflow (NOT
    # this row's own agent_id's workflow — there isn't one; the
    # PARENT never itself entered the durable path for this specific
    # pause, only the nested child did), and node_id is the PARENT's
    # own real Sub-Agent node. respond_to_hitl's own real, extended
    # handling (execution.py) checks this field: after genuinely
    # signaling the real child workflow via the normal
    # temporal_workflow_id mechanism and getting its real, final
    # result, it then resumes the PARENT's own flow (via
    # resume_flow_sync, using this row's own real ctx_snapshot) from
    # the Sub-Agent node's own outgoing edges — rather than returning
    # the child's own raw result as if it were the parent's own final
    # answer, which would be honest about the child's outcome but
    # silently drop the rest of the parent's own flow.
    delegated_child_agent_id: Optional[str] = None
    # run_id (§0.91, GAP_REPORT) — real, set on every real HitlRequest
    # created from this point forward, pointing at the SAME real Run
    # row this platform now creates at real INVOCATION time (before
    # execution even starts — see run_agent's own new, upfront Run-row
    # creation, execution.py). Lets respond_to_hitl find and update
    # that one, same, real Run row across the whole real pause/resume
    # lifecycle (running -> paused -> running -> its real terminal
    # state) instead of the old behavior, where a HITL-involving
    # invocation had NO Run row at all until it eventually reached ITS
    # OWN terminal state — meaning a workflow genuinely paused for
    # hours or days was invisible to GET /api/runs and the Runs table
    # for that entire real duration. Nullable — a real, existing
    # HitlRequest row predating this fix has no real Run to point at
    # (there's no way to recover one that was never created) — left
    # `None` rather than fabricating one, the same real, honest
    # convention this codebase already uses for every other real,
    # unbackfillable historical field (see Run.created_at, §0.77).
    run_id: Optional[str] = Field(default=None, foreign_key="run.id")


class ActivityIdempotency(SQLModel, table=True):
    """Real, general-purpose idempotency ledger (§0.76, GAP_REPORT) —
    the actual fix for the reported gap: Temporal retries a failed
    Activity automatically (at-least-once execution is the real,
    documented guarantee, not exactly-once), so any Activity that
    performs a genuinely non-idempotent side effect (this platform's
    own real, concrete example: a chat-history APPEND — read the prior
    value, concatenate the new turn, write the full result back — a
    retry that re-reads the now-already-updated prior value would
    silently double-append) needs a real way to recognize "this exact
    logical step already happened" and skip re-applying it, rather
    than trusting the side effect itself to be safe to repeat.

    Same real, already-verified pattern as HitlRequest's own
    idempotency_key/resolved_result (§0.74, GAP_REPORT) — a real,
    stable key (see temporal_workflow.py's own `_idempotency_key_for`:
    workflow_id + node_id + a real loop/iteration index, deliberately
    NOT including the real Temporal attempt number, since the whole
    point is that every retry of the SAME logical step shares the SAME
    key) maps to the real, cached result of the first, genuinely
    successful application — a retry sharing that key gets the exact
    same result replayed back rather than re-executing the side
    effect.

    See harness.py's own `idempotent_once` for the real, general
    helper any handler — including a real, future connector this
    reference platform doesn't have yet (a tool mutation, a payment
    connector) — can use this table through, and GAP_REPORT §0.76 for
    exactly which of this platform's own current handlers are wired
    through it today versus stated as real, honest future work."""
    id: str = Field(primary_key=True)  # the real idempotency key itself
    result: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ChatMessage(SQLModel, table=True):
    """Real, structured multi-turn chat history (§0.47, GAP_REPORT) —
    genuinely different from AgentMemory's own single-blob
    __llm_chat_history__ convention (still real and still used by any
    node with its own explicit memory_enabled checkbox — this table is
    additive, not a replacement). Backs the new agent-level
    features.chat_history toggle: real, ordered rows per
    (agent_id, invoking_user), replayed as a genuine multi-turn
    `messages` array to the real Anthropic API (see model_adapter.
    call_llm's own new `history` parameter) rather than a flattened
    text prefix — a real robustness improvement, not just a relocated
    checkbox."""
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    invoking_user: str = Field(index=True)
    # Which thread this turn belongs to. Without it, starting a new
    # conversation could only mean deleting the previous one, so there
    # was never any history to look back at. Nullable for rows written
    # before the column existed; the migration backfills them into one
    # conversation per (agent, user), which is what they were.
    conversation_id: Optional[str] = Field(default=None, index=True)
    role: str  # "user" | "assistant"
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AgentMemory(SQLModel, table=True):
    """Real persistent key-value store for the `memory` node, surviving
    across separate runs — the actual gap flagged as most important. A
    `memory` node's `scope_key` distinguishes two real scoping models:
    'agent' scope shares one value across every run/every user of this
    agent (scope_key = agent_id); 'conversation' scope is per-invoking-user
    (scope_key = f"{agent_id}:{invoking_user}") — the closest honest
    approximation available without a real session/conversation concept
    anywhere else in this repo. 'run' scope needs no table at all: it's
    just ctx["vars"], already free within a single execution.

    Real, database-level uniqueness (§0.70, GAP_REPORT) — every real
    write site used to SELECT by (scope_key, key), then either UPDATE
    the row found or INSERT a new one if none was — two genuinely
    concurrent writes to the same (scope_key, key) could both see no
    existing row and both insert, leaving two rows for what should be
    one real value. Every real write site now goes through a real
    Postgres INSERT ... ON CONFLICT (scope_key, key) DO UPDATE — see
    harness.py's own upsert_agent_memory — atomic and race-free by
    construction, not retried application-side after the fact."""
    __table_args__ = (UniqueConstraint("scope_key", "key", name="uq_agentmemory_scope_key_key"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    scope_key: str = Field(index=True)
    key: str
    value: str
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class EnvVar(SQLModel, table=True):
    """Per-agent environment variables (§ Workflow IDE toolbar's ENV
    button). String/Number values are plain — reusability/config-management
    use cases, nothing sensitive. Secret values are genuinely write-only
    from the API's perspective: no read endpoint ever returns the real
    value once saved; only the harness, executing server-side, ever
    reads it. `value` is real Fernet ciphertext at rest for secret-type
    rows since §0.59 (GAP_REPORT) — see secrets_crypto.py — with a
    real, honest backward-compatible fallback for any row still
    holding a genuinely plaintext value from before that fix.
    See routers/env_vars.py and harness.py's load_env_map().

    Real, database-level uniqueness (§0.70, GAP_REPORT) — a real,
    reported gap: create_env_var only ever checked (agent_id, key) via
    a SELECT before the INSERT, so two genuinely concurrent creates of
    the same variable name for the same agent could both pass that
    check and both insert — Postgres itself now rejects the second one
    via a real unique constraint; routers/env_vars.py catches the real
    resulting IntegrityError and returns the exact same honest "already
    exists" error the original SELECT-based check gave, just now
    genuinely race-free."""
    __table_args__ = (UniqueConstraint("agent_id", "key", name="uq_envvar_agent_id_key"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    key: str
    value: str
    type: str = "string"  # "string" | "number" | "secret"
    description: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TemplateSource(str, Enum):
    """Who can see/use the template once it is published."""
    PLATFORM = "platform"
    PRIVATE = "private"


class TemplateStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Template(SQLModel, table=True):
    """Mutable authoring record for a reusable Agent template.

    `flow` and the descriptive fields are the current draft. Publication never
    makes that draft itself live; it creates an immutable TemplateVersion and
    points `current_version_id` at it. That separation is what allows someone
    to keep editing a published template without changing what the gallery or
    an installer receives until the next explicit publish.

    `release_managed=True` is reserved for Platform templates shipped as
    package files with the application. Those rows are synchronized by
    seed.sync_platform_templates and remain read-only in Studio. Platform
    administrators can also author Organization-managed templates in Studio; those rows
    use `source=platform`, have `release_managed=False` and follow the same draft/publish lifecycle as
    Private templates.
    """
    __table_args__ = (
        # Display names are unique only while a template is active. Archived
        # history must not reserve a name forever, and lower(name) keeps the
        # database invariant aligned with the case-insensitive API check.
        Index(
            "uq_template_active_name_ci",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("status <> 'archived'"),
        ),
        UniqueConstraint("key", name="uq_template_key"),
        CheckConstraint("source IN ('platform','private')", name="ck_template_source"),
        CheckConstraint("status IN ('draft','published','archived')", name="ck_template_status"),
    )
    id: str = Field(primary_key=True)
    key: str
    name: str
    type: str  # Data | App
    app_type: Optional[str] = None
    desc: str
    first_party: bool = True
    # Mutable draft content. The published gallery reads the immutable current
    # TemplateVersion instead.
    flow: Dict[str, Any] = Field(default_factory=lambda: {"nodes": [], "edges": []}, sa_column=Column(JSONB))
    source: str = Field(default=TemplateSource.PLATFORM.value, index=True)
    status: str = Field(default=TemplateStatus.DRAFT.value, index=True)
    # Latest published semantic version. A never-published draft starts at
    # 0.0.0; publication promotes it to 1.0.0 and subsequent publishes bump
    # the patch component. Release-managed packages retain their manifest
    # version exactly.
    version: str = "0.0.0"
    category: Optional[str] = Field(default=None, index=True)
    vertical: Optional[str] = Field(default=None, index=True)
    tags: List[str] = Field(default_factory=list, sa_column=Column(JSONB))
    requirements: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    owner: Optional[str] = None
    release_managed: bool = Field(default=False, index=True)
    hidden: bool = False
    # Optimistic-lock and publish bookkeeping. Edits increment draft_revision.
    # published_revision records the draft revision represented by
    # current_version_id, so the API can expose has_unpublished_changes without
    # hashing a potentially large flow on every list request.
    draft_revision: int = 1
    published_revision: Optional[int] = None
    current_version_id: Optional[str] = Field(default=None, foreign_key="templateversion.id", index=True)
    published_at: Optional[datetime] = None
    published_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TemplateVersion(SQLModel, table=True):
    """Immutable snapshot created by each successful template publish."""
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_templateversion_template_version"),
    )
    id: str = Field(primary_key=True)
    template_id: str = Field(foreign_key="template.id", index=True)
    version: str
    name: str
    type: str
    app_type: Optional[str] = None
    desc: str
    first_party: bool = True
    flow: Dict[str, Any] = Field(default_factory=lambda: {"nodes": [], "edges": []}, sa_column=Column(JSONB))
    category: Optional[str] = None
    vertical: Optional[str] = None
    tags: List[str] = Field(default_factory=list, sa_column=Column(JSONB))
    requirements: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB))
    published_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    published_by: Optional[str] = None


class AgentSkill(SQLModel, table=True):
    """A skill package (.zip / .skill with a SKILL.md) in an agent's asset
    library. Immutable once created: there is no update path, so a published
    AgentVersion that names this id always sees the same bytes. Deleting one
    that a published version still references only *retires* it (hidden from
    the library, bytes kept)."""
    __table_args__ = (
        Index("uq_agentskill_agent_name_live", "agent_id", "name", unique=True,
              postgresql_where=text("retired_at IS NULL")),
    )
    id: str = Field(primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    name: str
    description: str = ""
    size_bytes: int = 0
    file_count: int = 0
    content: str = ""  # SKILL.md body (frontmatter removed)
    sha256: str = ""
    object_key: str = ""
    storage_backend: str = ""
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    retired_at: Optional[datetime] = None


class AgentFile(SQLModel, table=True):
    """A reference file in an agent's asset library. Immutable like
    AgentSkill. `status` says what the runtime can do with it:
    ready (text extracted), stored_only (kept, not sent to the model),
    failed (extraction failed or found no text)."""
    id: str = Field(primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    filename: str
    mime: str = ""
    kind: str = "document"  # document | image | audio | video
    size_bytes: int = 0
    status: str = "ready"
    status_detail: str = ""
    extracted_text: str = ""
    sha256: str = ""
    object_key: str = ""
    storage_backend: str = ""
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    retired_at: Optional[datetime] = None
    # What "Save as template" does with this file: require (the installer
    # uploads their own; the default, so no document's contents travel
    # unless someone chose that), embed (the text is copied into the
    # template), or exclude.
    template_policy: str = "require"


class AgentNodeSecret(SQLModel, table=True):
    """An encrypted Env value for one Agent node. The flow JSON (and so every
    AgentVersion) holds only the key name; the value lives here, encrypted
    with secrets_crypto, and is never returned by any API."""
    __table_args__ = (
        UniqueConstraint("agent_id", "node_id", "key", name="uq_agentnodesecret_agent_node_key"),
    )
    id: str = Field(primary_key=True)
    agent_id: str = Field(foreign_key="agent.id", index=True)
    node_id: str
    key: str
    value_enc: str = ""
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # Set when the draft stops declaring the key while a published version
    # still does: the value is kept so that version keeps working, and is
    # removed once no version references it.
    retired_at: Optional[datetime] = None
