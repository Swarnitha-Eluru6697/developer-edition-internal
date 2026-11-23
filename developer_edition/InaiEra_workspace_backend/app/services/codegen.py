"""Codegen service — the Workspace-embedded chat that generates SQL, dbt,
FlinkSQL, and Python code with a self-hosted, in-perimeter codegen AGENT.

Inference never goes to an external LLM provider: every model call goes
through the OpenAI-compatible gateway at CODEGEN_GATEWAY_URL (LiteLLM/vLLM in
front of open-weight checkpoints), addressed with logical model names
(codegen-generation / codegen-generation-alt / codegen-utility). There is no
provider API key anywhere in this service.

A turn is the harness in app/codegen_agent/: ground -> plan -> generate -> validate ->
(repair -> ...) -> deliver, with error routing and hard budgets — the model
proposes, the harness disposes. The per-target backends below (sql | python |
dbt | flinksql) own catalog access and validation; the harness drives them
through the CodegenBackend protocol.

Workspace behaviour kept unchanged around the harness:
  - Progress publishes onto the caller's live workspace-session channel
    (ws_session_id, WS-AI-011), so the open WebSocket delivers it.
  - A placeholder assistant row is written at submit time and updated in
    place, so a crashed turn is detectable and recoverable (WS-LIFE-013).
  - current_file_content arrives already filtered by the governance gate
    (app/services/codegen_governance.py); this service never re-decides it.

The agent only ever sees schema/metadata (table/column names, types, topic
schemas) — never row data. See _make_grounder().
"""
from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.codegen_agent import GroundingPack, Harness, HarnessDeps
from app.codegen_agent.contracts import ColumnMeta, KafkaTopicMeta, TableMeta
from app.codegen_agent.model_client import ModelClient
from app.core.config import get_settings
from app.services.clickhouse_catalog import ClickHouseCatalog, ClickHouseNotConfigured
from app.core.service_tokens import FLINK_AUDIENCE, FLINK_SCOPE_VALIDATE, post_with_service_token
from app.models import CodegenMessage, CodegenSession
from app.services.execution_router import get_redis, session_channel_for

log = structlog.get_logger()


# --------------------------------------------------------------------------
# Backends — one per target. Each owns context assembly + validation, i.e. the
# two things that are genuinely dialect-specific. Carried over from the previous
# design; the harness calls list_sources / get_table_schema / compile_code
# through the CodegenBackend protocol (no Claude-specific tool schema anymore).
# --------------------------------------------------------------------------
# The engines an SQL / dbt turn can run for.
SUPPORTED_SQL_ENGINES = ("pg_lake", "trino", "clickhouse")

_DIALECT_RULES = {
    "pg_lake": (
        "Dialect: PostgreSQL (the pg_lake warehouse). Use schema.table names. Casts may use `::` or CAST; `string_agg(x, ', ' ORDER BY x)`, "
        "`ILIKE`, `now()` and `generate_series` are available."
    ),
    "trino": (
        "Dialect: Trino SQL, not PostgreSQL. Name tables catalog.schema.table (`iceberg.<schema>.<table>` for the lakehouse, "
        "`postgresql.public.<table>` for the pg_lake warehouse's tables). Use CAST(x AS type), never `::`; "
        "`array_join(array_agg(x ORDER BY x), ', ')` instead of string_agg; `lower(a) LIKE lower(b)` instead of ILIKE; "
        "`current_timestamp` instead of now(); `date_trunc`, `date_format(ts, '%Y-%m-%d')` and `INTERVAL '1' DAY` for dates; "
        "`decimal(p, s)` for exact numbers; there is no transaction across statements."
    ),
    "clickhouse": (
        "Dialect: ClickHouse SQL, not PostgreSQL. Name tables database.table. Use CAST(x AS Type) or toInt64(x) / toString(x) / toDate(x), "
        "never `::`; types are case-sensitive (String, Int64, UInt32, Float64, Decimal(p, s), DateTime, Date, Nullable(T), LowCardinality(String)). "
        "`arrayStringConcat(groupArray(x), ', ')` instead of string_agg; `ilike`/`like`; `now()` and `today()`; `toStartOfDay(ts)`, "
        "`toStartOfMonth(ts)`, `formatDateTime(ts, '%Y-%m-%d')`, `ts + INTERVAL 1 DAY` for dates; `count()`, `uniq(x)`, `countIf(cond)`, "
        "`sumIf(x, cond)`. A table needs an ENGINE (MergeTree) and an ORDER BY / PRIMARY KEY: `CREATE TABLE db.t (...) ENGINE = MergeTree "
        "ORDER BY (...)`. There are no transactions or UPDATE-in-place; for a dbt model prefer a view or a table materialization, "
        "and give an incremental model a unique_key and an ORDER BY."
    ),
}


def dialect_rules(engine: str) -> str:
    """Short, engine-specific SQL rules appended to the SQL and dbt instructions ("" for an engine without any)."""
    return _DIALECT_RULES.get(engine, "")


def resolve_turn_engine(target: str, requested: str | None) -> str:
    """The engine a turn runs for: the user's choice for sql/dbt when it is a supported SQL engine, else the target's own default
    (python and flinksql have one engine each, so a requested engine never applies to them)."""
    if target in ("sql", "dbt") and requested in SUPPORTED_SQL_ENGINES:
        return requested  # type: ignore[return-value]
    return get_backend(target).default_engine


class CodegenBackend(ABC):
    target: str
    default_engine: str

    @abstractmethod
    def system_prompt(self, *, engine: str, project_context: str) -> str: ...

    @abstractmethod
    async def list_sources(self, *, namespace: str | None) -> dict: ...

    @abstractmethod
    async def get_table_schema(self, *, table: str) -> dict: ...

    @abstractmethod
    async def compile_code(self, *, code: str, path: str, companion_code: str | None, engine: str | None = None) -> dict: ...


class _LakehouseCatalogMixin:
    """Shared catalog calls — SQL, dbt, and Python backends read the same Data
    Lake House catalog endpoint (GET /v1/catalog/*)."""

    async def _catalog_list(self, namespace: str | None) -> dict:
        settings = get_settings()
        params = {"namespace": namespace} if namespace else {}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{settings.lakehouse_query_url}/v1/catalog/tables", params=params)
            resp.raise_for_status()
            return resp.json()

    async def _catalog_schema(self, table: str) -> dict:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{settings.lakehouse_query_url}/v1/catalog/tables/{table}/schema")
            resp.raise_for_status()
            return resp.json()

    async def _lakehouse_explain(self, sql: str) -> dict:
        """Ask the lakehouse query service to EXPLAIN the SQL. A 4xx that carries a message is the SQL's own fault (a real validation failure).
        But when the service itself cannot answer — no such endpoint (404/405/501), a 5xx, or no connection — there is nothing wrong with the
        SQL to report: the check is SKIPPED, not failed (a missing validator used to make every candidate "fail", so the assistant kept
        regenerating correct SQL until it gave up)."""
        settings = get_settings()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{settings.lakehouse_query_url}/v1/queries/explain",
                    json={"sql": sql},
                )
        except httpx.HTTPError as e:
            return {"ok": True, "skipped": True, "errors": [], "reason": f"query service unreachable: {type(e).__name__}"}
        if resp.status_code in (404, 405, 501) or resp.status_code >= 500:
            return {"ok": True, "skipped": True, "errors": [], "reason": f"query service cannot validate (HTTP {resp.status_code})"}
        if resp.status_code >= 400:
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            return {"ok": False, "errors": [body.get("message", resp.text[:500])]}
        return {"ok": True, "errors": []}


class SqlBackend(_LakehouseCatalogMixin, CodegenBackend):
    target = "sql"
    default_engine = "trino"        # Trino is the engine offered everywhere now; pg_lake is still accepted when asked for (schemas.codegen.SqlEngine)

    def system_prompt(self, *, engine: str, project_context: str) -> str:
        return (
            "You are the InAiEra Workspace SQL assistant. You write ad hoc, "
            f"read-mostly SQL for the {engine} engine against Iceberg tables in "
            "the lakehouse, in the dialect that engine speaks. Use only the "
            "tables and columns given in the confirmed context — never invent a "
            "schema. Output a single runnable statement unless the user asks for "
            "a script.\n\n"
            f"{dialect_rules(engine)}\n\n"
            f"Project context:\n{project_context}"
        )

    async def list_sources(self, *, namespace: str | None) -> dict:
        return await self._catalog_list(namespace)

    async def get_table_schema(self, *, table: str) -> dict:
        return await self._catalog_schema(table)

    async def compile_code(self, *, code: str, path: str, companion_code: str | None, engine: str | None = None) -> dict:
        if engine == "clickhouse":
            return await ClickHouseCatalog().explain(code)
        return await self._lakehouse_explain(code)


class DbtBackend(_LakehouseCatalogMixin, CodegenBackend):
    target = "dbt"
    default_engine = "trino"        # also what the dbt Runner validates the generated model against (compile_code)

    def system_prompt(self, *, engine: str, project_context: str) -> str:
        return (
            "You are the InAiEra Workspace dbt assistant. You write dbt models "
            f"targeting {engine} against Iceberg tables in the lakehouse, "
            "following the existing project's conventions (naming, "
            "materialization, folder layout: models/staging, models/intermediate, "
            "models/marts). Use {{ source(...) }} for raw tables and {{ ref(...) }} "
            "for other models in this project — never hardcode table names. Use "
            "only sources present in the confirmed context. Produce a model SQL "
            "file and, when columns warrant it, a companion schema.yml with "
            "column tests (not_null, unique) inferred from naming and "
            "constraints you can see.\n\n"
            f"{dialect_rules(engine)}\n\n"
            f"Project context (existing models, conventions):\n{project_context}"
        )

    async def list_sources(self, *, namespace: str | None) -> dict:
        return await self._catalog_list(namespace)

    async def get_table_schema(self, *, table: str) -> dict:
        return await self._catalog_schema(table)

    async def compile_code(self, *, code: str, path: str, companion_code: str | None, engine: str | None = None) -> dict:
        """dbt Runner validate-only endpoint — parse + compile + engine dry-run,
        no materialization. Workspace never runs dbt itself."""
        settings = get_settings()
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.dbtrunner_url}/api/v1/validate",
                headers={"X-Internal-Service-Token": settings.dbtrunner_internal_token or ""},
                json={
                    "path": path,
                    "sql": code,
                    "schema_yml": companion_code,
                    "engine": engine or self.default_engine,
                },
            )
            if resp.status_code >= 400:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                return {"ok": False, "errors": body.get("errors", [resp.text[:500]])}
            data = resp.json()
            return {"ok": data.get("valid", False), "errors": data.get("errors", [])}


class FlinkSqlBackend(_LakehouseCatalogMixin, CodegenBackend):
    """Gated by settings.codegen_flinksql_enabled until the Dataconnect
    topic/schema catalog integration lands (design §14 Phase 4)."""

    target = "flinksql"
    default_engine = "flink"

    def system_prompt(self, *, engine: str, project_context: str) -> str:
        return (
            "You are the InAiEra Workspace FlinkSQL assistant. You write "
            "streaming FlinkSQL jobs reading from Kafka topics ingested by "
            "Dataconnect and writing to Iceberg tables in the lakehouse. Use "
            "only topics and tables present in the confirmed context, and only "
            "connector WITH options from the provided allowlist — never invent "
            "connector options. Be explicit about changelog mode (append vs "
            "upsert).\n\n"
            f"Project context:\n{project_context}"
        )

    async def list_sources(self, *, namespace: str | None) -> dict:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{settings.lakehouse_query_url}/v1/catalog/streams",
                params={"namespace": namespace} if namespace else {},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_table_schema(self, *, table: str) -> dict:
        return await self._catalog_schema(table)

    async def compile_code(self, *, code: str, path: str, companion_code: str | None, engine: str | None = None) -> dict:
        settings = get_settings()
        if not settings.flinksql_internal_token:
            return {"ok": True, "errors": [], "skipped": True,
                    "note": "FlinkSQL runner not configured — code returned without engine validation."}
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await post_with_service_token(
                    client, f"{settings.flinksql_runner_url}/api/v1/validate",
                    json={"sql": code},
                    signing_secret=settings.flinksql_internal_token,
                    audience=FLINK_AUDIENCE, scope=FLINK_SCOPE_VALIDATE,
                    legacy_token=settings.flinksql_internal_token,
                )
            if resp.status_code >= 400:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                return {"ok": False, "errors": body.get("errors", [resp.text[:500]])}
            data = resp.json()
            return {
                "ok": data.get("valid", False),
                "errors": data.get("errors", []),
                "detected_sources": data.get("detected_sources", []),
                "detected_sinks": data.get("detected_sinks", []),
            }
        except httpx.HTTPError as e:
            return {"ok": False, "errors": [f"FlinkSQL runner unreachable: {e}"]}


class PythonBackend(_LakehouseCatalogMixin, CodegenBackend):
    """Generates general-purpose Python (Kafka produce/consume, file
    processing, data manipulation). Validation is static-only and NEVER runs
    the code (design §7.5) — the harness routes python to the static ladder in
    app/codegen_agent/python_checks.py, so compile_code here is a safe
    syntax-only fallback kept for protocol completeness."""

    target = "python"
    default_engine = "python"

    def system_prompt(self, *, engine: str, project_context: str) -> str:
        return (
            "You are the InAiEra Workspace Python assistant. You write clear, "
            "runnable Python for data work — pandas/pyarrow, the confluent-kafka "
            "client for Kafka, and standard library — meant to run in a notebook "
            "cell or .py file. Import only well-known data libraries; never use "
            "subprocess, sockets, eval/exec, or pickle on external data. When a "
            "Kafka topic is involved, use the topic names and registered schemas "
            "from the confirmed context, and state your delivery-semantics "
            "assumptions (at-least-once vs exactly-once, offset handling). When "
            "editing existing code, propose the full edited version.\n\n"
            f"Project context:\n{project_context}"
        )

    async def list_sources(self, *, namespace: str | None) -> dict:
        return await self._catalog_list(namespace)

    async def get_table_schema(self, *, table: str) -> dict:
        return await self._catalog_schema(table)

    async def compile_code(self, *, code: str, path: str, companion_code: str | None, engine: str | None = None) -> dict:
        try:
            compile(code, path or "<cell>", "exec")
            return {"ok": True, "errors": []}
        except SyntaxError as e:
            return {"ok": False, "errors": [f"{e.msg} (line {e.lineno})"]}


_BACKENDS: dict[str, CodegenBackend] = {
    "sql": SqlBackend(),
    "python": PythonBackend(),
    "dbt": DbtBackend(),
    "flinksql": FlinkSqlBackend(),
}


def get_backend(target: str) -> CodegenBackend:
    backend = _BACKENDS.get(target)
    if backend is None:
        raise ValueError(f"unknown codegen target: {target}")
    return backend


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
class CodegenService:
    """Runs the chat turn: builds harness deps, runs the harness, persists the
    transcript, streams progress over the same WS envelope shape executions
    use. The heavy lifting (ground/plan/generate/validate/repair) lives in
    app/codegen_agent/harness.py."""

    def __init__(self) -> None:
        self._model = ModelClient()  # one OpenAI-compatible gateway client, reused
        self._tasks: set = set()  # strong refs so turn tasks aren't GC'd mid-run

    async def submit_turn(
        self,
        db: AsyncSession,
        *,
        session: CodegenSession,
        ws_session_id: uuid.UUID,
        prompt: str,
        current_file_content: str | None,
        project_context: str,
        target: str | None = None,
        model_name: str | None = None,
        connection_id: uuid.UUID | None = None,
        principal: Any = None,
        engine: str | None = None,
    ) -> CodegenMessage:
        """Persist the user's message, kick off the harness in the background,
        return immediately. Mirrors ExecutionRouter.submit.

        `target` overrides the session's target for THIS turn, so the user can
        switch sql/python/dbt/flinksql mid-conversation without a new session.
        `model_name` optionally picks the generation model (one of
        settings.codegen_generation_models); anything else -> the default.

        `ws_session_id` is the caller's live workspace Session id — the one its
        WebSocket is connected to. Progress publishes onto THAT session's Redis
        channel (WS-AI-011). `current_file_content` is whatever the governance
        gate already decided is safe to forward — not re-checked here.
        """
        turn_target = target or session.target
        user_msg = CodegenMessage(
            session_id=session.id,
            role="user",
            content=[{"type": "text", "text": prompt}],
        )
        db.add(user_msg)
        # Placeholder assistant row, written up front and updated in place once
        # the turn finishes (WS-LIFE-013): a mid-turn crash leaves a 'running'
        # row the LifecycleReconciler can find and fail out, instead of a user
        # prompt with no reply forever.
        assistant_msg = CodegenMessage(
            session_id=session.id,
            role="assistant",
            content=[],
            status="running",
            ws_session_id=ws_session_id,
        )
        db.add(assistant_msg)
        await db.flush()
        assistant_message_id = assistant_msg.id

        import asyncio
        task = asyncio.create_task(
            self._run_turn(
                codegen_session_id=session.id,
                ws_session_id=ws_session_id,
                assistant_message_id=assistant_message_id,
                target=turn_target,
                engine=resolve_turn_engine(turn_target, engine),
                prompt=prompt,
                current_file_content=current_file_content,
                project_context=project_context,
                model_name=self._resolve_model(model_name),
                connection_id=connection_id,
                principal=principal,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return user_msg

    def _resolve_model(self, model_name: str | None) -> str:
        """Map a user-selected generation model to a logical gateway name,
        defaulting to the platform default and rejecting anything not on the
        configured allowlist (so a client can't point us at an arbitrary model
        string)."""
        settings = get_settings()
        if model_name and model_name in set(settings.codegen_generation_models):
            return model_name
        return settings.codegen_model_generation

    async def _run_turn(
        self,
        *,
        codegen_session_id: uuid.UUID,
        ws_session_id: uuid.UUID,
        assistant_message_id: uuid.UUID,
        target: str,
        engine: str,
        prompt: str,
        current_file_content: str | None,
        project_context: str,
        model_name: str,
        connection_id: uuid.UUID | None = None,
        principal: Any = None,
    ) -> None:
        from app.db.session import AsyncSessionLocal  # local import to avoid cycle

        r = get_redis()
        backend = get_backend(target)

        async def publish(msg: dict[str, Any]) -> None:
            # `session_id` is the codegen session's id — the client matches the
            # envelope to its in-flight turn with it; the CHANNEL is the live
            # workspace session (see _publish).
            await self._publish(r, ws_session_id, {"session_id": str(codegen_session_id), **msg})

        await publish({"type": "codegen.started"})

        deps = HarnessDeps(
            model=self._model,
            backend=backend,
            publish=publish,
            engine=engine,
            project_context=project_context,
            ground=self._make_grounder(backend, connection_id=connection_id, principal=principal, engine=engine),
        )

        try:
            outcome = await Harness(deps).run(
                target=target,  # type: ignore[arg-type]
                prompt=prompt,
                current_file_content=current_file_content,
                model_name=model_name,
            )

            # Surface the plan's assumptions to the reviewer (design §8.1).
            reply_text = outcome.reply_text
            if outcome.assumptions:
                reply_text += "\n\nAssumptions:\n" + "\n".join(f"  - {a}" for a in outcome.assumptions)

            async with AsyncSessionLocal() as db:
                # Update the placeholder written at submit time (WS-LIFE-013).
                assistant_msg = await db.get(CodegenMessage, assistant_message_id)
                assistant_msg.content = [{"type": "text", "text": reply_text}]
                assistant_msg.generated_code = outcome.artifact_code
                assistant_msg.generated_path = outcome.artifact_path
                assistant_msg.validation_status = outcome.validation_status
                assistant_msg.validation_detail = outcome.validation_detail
                assistant_msg.model = outcome.model
                assistant_msg.input_tokens = outcome.input_tokens
                assistant_msg.output_tokens = outcome.output_tokens
                assistant_msg.status = "completed"
                await db.commit()

            await publish({
                "type": "codegen.completed",
                "message_id": str(assistant_message_id),
                "reply_text": reply_text,
                "artifact_path": outcome.artifact_path,
                "artifact_code": outcome.artifact_code,
                "companion_path": outcome.companion_path,
                "companion_code": outcome.companion_code,
                "validation_status": outcome.validation_status,
                "validation_detail": outcome.validation_detail,
                "model": outcome.model,
            })

        except Exception as e:
            log.exception("codegen_turn_failed", session_id=str(codegen_session_id))
            error_message = _failure_message(e)
            try:
                async with AsyncSessionLocal() as db:
                    assistant_msg = await db.get(CodegenMessage, assistant_message_id)
                    if assistant_msg and assistant_msg.status == "running":
                        assistant_msg.status = "failed"
                        assistant_msg.content = [{"type": "text", "text": error_message}]
                        await db.commit()
            except Exception:
                # Left 'running': the LifecycleReconciler (WS-LIFE-013) still
                # fails it out once stale — this is a backstop, not the only path.
                log.exception("codegen_turn_failure_persist_failed", session_id=str(codegen_session_id))
            await publish({"type": "codegen.failed", "error_message": error_message})

    # ------------------------------------------------------------------
    # Grounding (§5.3) — assemble a GroundingPack from the lakehouse catalog and
    # (for Kafka-touching targets) the schema registry. Metadata only; never row
    # data. Returns a fresh, immutable pack each call (§4.5).
    # ------------------------------------------------------------------
    def _make_grounder(self, backend: CodegenBackend, *, connection_id: uuid.UUID | None = None, principal: Any = None, engine: str | None = None):
        async def ground(prompt: str, target: str, external_systems: list[str]) -> GroundingPack:
            tables: list[TableMeta] = []
            kafka: list[KafkaTopicMeta] = []
            pii: list[str] = []

            # The connection the user is working against comes FIRST: its real tables and columns, from the same governed catalog the
            # Object Explorer shows. Without this the assistant only saw the generic lakehouse catalog (which does not contain the
            # user's database) and invented columns.
            if connection_id is not None and principal is not None:
                tables = await ground_from_connection(connection_id, principal, prompt)

            # Candidate tables: list_sources gives names; hydrate a bounded
            # shortlist by fetching column schema for those whose name tokens
            # appear in the prompt (cheap relevance filter — the design's full
            # BM25/embedding retrieval can slot in here later without changing
            # the pack shape).
            # A ClickHouse turn reads its tables from ClickHouse itself (the lakehouse catalog does not know them).
            catalog: Any = backend
            if engine == "clickhouse" and target in ("sql", "dbt"):
                try:
                    ch = ClickHouseCatalog()
                except ClickHouseNotConfigured as e:
                    log.warning("grounding_clickhouse_not_configured", error=str(e))
                    ch = None
                catalog = _ClickHouseSources(ch) if ch else _NoSources()
            try:
                listing = await catalog.list_sources(namespace=None) if not tables else []
                for fqn in _candidate_names(listing, prompt, limit=8):
                    try:
                        schema = await catalog.get_table_schema(table=fqn)
                    except httpx.HTTPError:
                        continue
                    cols = [
                        ColumnMeta(
                            name=c.get("name", ""),
                            type=c.get("type"),
                            nullable=c.get("nullable"),
                            tags=c.get("tags", []) or [],
                        )
                        for c in schema.get("columns", [])
                    ]
                    pii.extend(f"{fqn}.{c.name}" for c in cols if "PII" in (c.tags or []))
                    tables.append(TableMeta(
                        fqn=fqn,
                        columns=cols,
                        partition_keys=schema.get("partition_keys", []) or [],
                        description=schema.get("description"),
                    ))
            except httpx.HTTPError as e:
                log.warning("grounding_catalog_unreachable", error=str(e))

            if "kafka" in external_systems or target == "flinksql":
                kafka = await self._ground_kafka(prompt)

            return GroundingPack(
                target=target,  # type: ignore[arg-type]
                tables=tables,
                kafka_topics=kafka,
                pii_columns=sorted(set(pii)),
                write_allowlist=[],  # populated from workspace policy when available
                external_systems=external_systems,
            )

        return ground

    async def _ground_kafka(self, prompt: str) -> list[KafkaTopicMeta]:
        """Pull candidate topic schemas from the schema registry, if configured.
        Degrades to empty (advisory) when unset — the L2 kafka bind then simply
        can't verify topics, which is recorded rather than blocking."""
        settings = get_settings()
        if not settings.schema_registry_url:
            return []
        out: list[KafkaTopicMeta] = []
        auth = None
        if settings.schema_registry_auth and ":" in settings.schema_registry_auth:
            u, p = settings.schema_registry_auth.split(":", 1)
            auth = (u, p)
        try:
            async with httpx.AsyncClient(timeout=15.0, auth=auth) as client:
                subs = await client.get(f"{settings.schema_registry_url}/subjects")
                subs.raise_for_status()
                for subject in _candidate_names(subs.json(), prompt, limit=8):
                    try:
                        latest = await client.get(
                            f"{settings.schema_registry_url}/subjects/{subject}/versions/latest"
                        )
                        latest.raise_for_status()
                    except httpx.HTTPError:
                        continue
                    body = latest.json()
                    out.append(KafkaTopicMeta(
                        topic=subject.removesuffix("-value").removesuffix("-key"),
                        schema_type=str(body.get("schemaType", "avro")).lower(),
                        schema_str=body.get("schema"),
                        subject=subject,
                    ))
        except httpx.HTTPError as e:
            log.warning("grounding_registry_unreachable", error=str(e))
        return out

    @staticmethod
    async def _publish(r, ws_session_id: uuid.UUID, msg: dict) -> None:
        """Publish onto the caller's live workspace-session channel — the same
        one the /ws/sessions/{id} gateway subscribes to (WS-AI-011)."""
        envelope = {"v": 1, "ts": datetime.now(timezone.utc).isoformat(), **msg}
        await r.publish(session_channel_for(str(ws_session_id)), json.dumps(envelope))


def _failure_message(e: Exception) -> str:
    """User-facing reason for a failed turn. An unreachable model gateway is
    the common local-dev failure, so name it instead of a raw httpx string."""
    if isinstance(e, httpx.ConnectError):
        return (
            "The codegen model gateway is unreachable at "
            f"{get_settings().codegen_gateway_url} — start it (LiteLLM/vLLM) "
            "or set CODEGEN_GATEWAY_URL."
        )
    if isinstance(e, httpx.HTTPStatusError):
        return f"The codegen model gateway returned HTTP {e.response.status_code}: {e.response.text[:300]}"
    return str(e)[:500]


# --------------------------------------------------------------------------
# Grounding relevance helpers
# --------------------------------------------------------------------------
_SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast"})
_MAX_NAMESPACES = 12
_MAX_TABLES_PER_NAMESPACE = 400
_GROUNDING_TABLES = 8


def rank_connection_tables(tables: list[tuple[str, str]], prompt: str, *, limit: int = _GROUNDING_TABLES) -> list[tuple[str, str]]:
    """Which (schema, table) pairs of a connection to describe to the model. A table the prompt names (`dim_customers`) always wins, then
    tables sharing words with it; with no match at all the first few (user schemas first) so the model is never left without a real schema."""
    import re
    words = set(re.findall(r"[a-z0-9_]+", prompt.lower()))
    text = prompt.lower()

    def score(item: tuple[str, str]) -> int:
        ns, table = item
        t = table.lower()
        qualified = 5 if f"{ns.lower()}.{t}" in text else 0          # the prompt said exactly which schema
        named = (10 if t in words else 0) + qualified
        parts = sum(1 for tok in re.split(r"[_\W]+", t) if tok and tok in words)
        return named + parts

    ranked = sorted(tables, key=lambda it: (-score(it), it[0] != "public", it[0], it[1]))
    return ranked[:limit]


async def ground_from_connection(connection_id: uuid.UUID, principal: Any, prompt: str) -> list[TableMeta]:
    """Real tables and columns of the user's connection, via the Object Explorer's catalog adapter (UCS grants apply to `principal`).
    Metadata only. Any failure returns [] (the caller falls back to the lakehouse catalog) — grounding must never break a turn."""
    from app.db.session import AsyncSessionLocal
    from app.models import DatabaseConnection
    from app.services.catalog_adapter import get_catalog_adapter

    try:
        async with AsyncSessionLocal() as db:
            conn = await db.get(DatabaseConnection, connection_id)
            if conn is None:
                return []
            db.expunge(conn)
        adapter = get_catalog_adapter()
        namespaces = [n.name for n in await adapter.list_namespaces(conn, principal) if n.name.lower() not in _SYSTEM_SCHEMAS][:_MAX_NAMESPACES]
        pairs: list[tuple[str, str]] = []
        for ns in namespaces:
            try:
                pairs += [(ns, t.name) for t in (await adapter.list_tables(conn, ns, principal))[:_MAX_TABLES_PER_NAMESPACE]]
            except Exception as e:  # noqa: BLE001 — one unreadable schema must not hide the others
                log.warning("grounding_namespace_unavailable", connection_id=str(connection_id), namespace=ns, error=str(e)[:200])
        tables: list[TableMeta] = []
        for ns, table in rank_connection_tables(pairs, prompt):
            try:
                cols = await adapter.list_columns(conn, ns, table, principal)
            except Exception as e:  # noqa: BLE001
                log.warning("grounding_columns_unavailable", connection_id=str(connection_id), table=f"{ns}.{table}", error=str(e)[:200])
                continue
            if cols:
                tables.append(TableMeta(
                    fqn=f"{ns}.{table}",
                    columns=[ColumnMeta(name=c.name, type=c.data_type, nullable=c.is_nullable) for c in cols],
                ))
        return tables
    except Exception as e:  # noqa: BLE001
        log.warning("grounding_connection_unavailable", connection_id=str(connection_id), error=str(e)[:200])
        return []


def _candidate_names(listing: Any, prompt: str, *, limit: int) -> list[str]:
    """Cheap relevance filter over catalog table names or registry subjects.
    Prefers names whose tokens appear in the prompt; falls back to the first N
    so grounding is never empty when a catalog exists."""
    names = _extract_names(listing)
    if not names:
        return []
    p = prompt.lower()

    def score(n: str) -> int:
        toks = n.lower().replace(".", " ").replace("_", " ").replace("-", " ").split()
        return sum(tok in p for tok in toks)

    scored = sorted(names, key=score, reverse=True)
    hits = [n for n in scored if score(n) > 0]
    return (hits or scored)[:limit]


def _extract_names(listing: Any) -> list[str]:
    if isinstance(listing, list):
        if listing and isinstance(listing[0], dict):
            return [x.get("fqn") or x.get("name") for x in listing if x.get("fqn") or x.get("name")]
        return [str(x) for x in listing]
    if isinstance(listing, dict):
        for key in ("tables", "streams", "subjects", "items", "results"):
            if key in listing and isinstance(listing[key], list):
                return _extract_names(listing[key])
    return []


class _ClickHouseSources:
    """The grounder's view of ClickHouse: the same two calls a CodegenBackend offers."""
    def __init__(self, catalog: ClickHouseCatalog) -> None:
        self._c = catalog

    async def list_sources(self, *, namespace: str | None) -> dict:
        return await self._c.list_tables()

    async def get_table_schema(self, *, table: str) -> dict:
        return await self._c.table_schema(table)


class _NoSources:
    async def list_sources(self, *, namespace: str | None) -> dict:
        return {"tables": []}

    async def get_table_schema(self, *, table: str) -> dict:
        return {"columns": []}


_service: CodegenService | None = None


def get_codegen_service() -> CodegenService:
    global _service
    if _service is None:
        _service = CodegenService()
    return _service
