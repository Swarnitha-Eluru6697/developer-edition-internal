"""The harness — orchestrates one codegen turn as a bounded state machine.

    ground -> plan -> generate -> validate -> (repair -> ...) -> deliver

Design principles enforced here (agent design §2):
  * The model proposes, the harness disposes — nothing is delivered unless the
    validation ladder passes (or the turn is a pure "explain" with no code).
  * Ground before you generate; plan before you write.
  * Fail fast, cheapest rung first (the ladder lives in the backend + python_checks).
  * Route failures to the stage that caused them (Diagnostic.route_to).
  * Bound everything: attempts per stage, total attempts, loop detection.
  * On exhaustion, return a diagnosis — never a best-effort invalid artifact.

The harness is deliberately decoupled from persistence and streaming: it takes
a `publish` callback and returns a HarnessOutcome. app/services/codegen.py owns
the DB writes and Redis envelopes, exactly as before, so the API/WS/apply
surface is unchanged.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

import structlog

from app.codegen_agent.contracts import (
    Diagnostic,
    ErrorClass,
    GroundingPack,
    Plan,
    RepairRoute,
    RungResult,
    Target,
    ValidationReport,
)
from app.codegen_agent.model_client import ModelClient, extract_code, extract_json
from app.codegen_agent.python_checks import validate_python_notebook
from app.core.config import get_settings

log = structlog.get_logger()

Publisher = Callable[[dict[str, Any]], Awaitable[None]]


class CodegenBackend(Protocol):
    """The per-target collaborator the harness drives. Satisfied by the
    existing SqlBackend/DbtBackend/FlinkSqlBackend/PythonBackend in
    app/services/codegen.py — they already own catalog access and validation."""

    target: str
    default_engine: str

    def system_prompt(self, *, engine: str, project_context: str) -> str: ...
    async def list_sources(self, *, namespace: str | None) -> dict: ...
    async def get_table_schema(self, *, table: str) -> dict: ...
    async def compile_code(self, *, code: str, path: str, companion_code: str | None, engine: str | None = None) -> dict: ...


@dataclass
class HarnessDeps:
    model: ModelClient
    backend: CodegenBackend
    publish: Publisher
    engine: str
    project_context: str
    # grounding hook: given the user prompt, produce a GroundingPack. Injected so
    # the grounding strategy (catalog retrieval, schema registry) can evolve
    # without touching the harness. Defaults are provided by the service layer.
    ground: Callable[[str, Target, list[str]], Awaitable[GroundingPack]]


@dataclass
class HarnessOutcome:
    reply_text: str
    artifact_code: str | None = None
    artifact_path: str | None = None
    companion_code: str | None = None
    companion_path: str | None = None
    validation_status: str = "skipped"  # passed | failed | skipped
    validation_detail: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    # assumptions surfaced to the user (§8.1) — especially important for python
    assumptions: list[str] = field(default_factory=list)


class Harness:
    def __init__(self, deps: HarnessDeps) -> None:
        self.d = deps
        self.s = get_settings()
        self._in = 0
        self._out = 0
        self._model_used = ""
        self._seen_hashes: set[str] = set()  # loop detection (§9.2)
        self._stage_attempts: dict[str, int] = {"GROUND": 0, "PLAN": 0, "GENERATE": 0}

    async def run(
        self,
        *,
        target: Target,
        prompt: str,
        current_file_content: str | None,
        model_name: str,
    ) -> HarnessOutcome:
        """Execute one turn. `model_name` is the logical generation model the
        user selected (codegen-generation / -alt)."""
        self._model_used = model_name

        # ---- GROUND -----------------------------------------------------
        external = _guess_external_systems(prompt, target)
        grounding = await self.d.ground(prompt, target, external)
        await self.d.publish({"type": "codegen.agent_step", "stage": "ground",
                              "tables": len(grounding.tables),
                              "kafka_topics": len(grounding.kafka_topics)})

        # ---- PLAN -------------------------------------------------------
        plan = await self._plan(target, prompt, grounding, model_name)
        await self.d.publish({"type": "codegen.agent_step", "stage": "plan",
                              "intent": (plan.intent if plan else None)})

        # ---- GENERATE + VALIDATE + REPAIR (bounded loop) ----------------
        artifact_code: str | None = None
        artifact_path: str | None = None
        companion_code: str | None = None
        companion_path: str | None = None
        report = ValidationReport(passed=False, rungs=[])
        last_diag: Diagnostic | None = None
        final_text = ""
        repair_note: str | None = None

        for attempt in range(self.s.codegen_max_agent_turns):
            gen = await self._generate(
                target=target, prompt=prompt, grounding=grounding, plan=plan,
                current_file_content=current_file_content, model_name=model_name,
                repair=last_diag, attempt=attempt,
            )
            final_text = gen["text"]
            code = gen["code"]
            path = gen["path"]
            companion = gen["companion"]

            # Pure explanation / no code — deliver as-is (nothing to validate).
            if not code:
                report = ValidationReport(passed=True, rungs=[])  # nothing to validate
                artifact_code = None
                break

            # Loop detection: same failing candidate twice -> stop retrying here.
            h = _hash(code)
            if h in self._seen_hashes:
                await self.d.publish({"type": "codegen.agent_step", "stage": "loop_detected",
                                      "attempt": attempt})
                repair_note = "Repeated a previously-failed candidate; stopping to avoid a loop."
                break
            self._seen_hashes.add(h)

            # ---- VALIDATE ----------------------------------------------
            report = await self._validate(
                target=target, code=code, path=path,
                companion=companion, grounding=grounding,
            )
            await self._publish_validation(report, attempt)

            artifact_code, artifact_path = code, path
            companion_code, companion_path = companion, gen.get("companion_path")

            if report.passed:
                break

            # ---- ROUTE THE FAILURE (§9) --------------------------------
            last_diag = report.first_failure
            if last_diag is None:
                break

            route = last_diag.route_to
            if route is RepairRoute.TERMINAL:
                repair_note = _terminal_note(last_diag)
                break

            # per-stage budget
            self._stage_attempts[route.value] = self._stage_attempts.get(route.value, 0) + 1
            if self._stage_attempts[route.value] > self.s.codegen_max_attempts_per_stage:
                repair_note = (
                    f"Exhausted repair attempts at the {route.value.lower()} stage "
                    f"for: {last_diag.message}"
                )
                break

            await self.d.publish({"type": "codegen.agent_step", "stage": "repair",
                                  "route_to": route.value,
                                  "error_class": last_diag.error_class.value,
                                  "attempt": attempt})

            # Re-ground: a new pack (immutable-pack rule §4.5), then regenerate.
            if route is RepairRoute.GROUND and last_diag.offending_identifier:
                grounding = await self.d.ground(
                    prompt, target,
                    list({*external, last_diag.offending_identifier}),
                )
            # PLAN / GENERATE routes just loop with last_diag attached to the
            # next generate() call; PLAN additionally refreshes the plan.
            if route is RepairRoute.PLAN:
                plan = await self._plan(target, prompt, grounding, model_name, repair=last_diag)

        else:
            # for-loop exhausted without break -> budget exhausted
            repair_note = (
                "I couldn't reach a validated artifact within the attempt budget."
            )

        # ---- DELIVER / DIAGNOSE ----------------------------------------
        reply_text = _compose_reply(final_text, report, repair_note, last_diag)

        return HarnessOutcome(
            reply_text=reply_text,
            artifact_code=artifact_code,
            artifact_path=artifact_path,
            companion_code=companion_code,
            companion_path=companion_path,
            validation_status=report.status,
            validation_detail=report.as_detail(),
            model=self._model_used,
            input_tokens=self._in,
            output_tokens=self._out,
            assumptions=(plan.assumptions if plan else []),
        )

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------
    async def _plan(
        self,
        target: Target,
        prompt: str,
        grounding: GroundingPack,
        model_name: str,
        *,
        repair: Diagnostic | None = None,
    ) -> Plan | None:
        """Emit a structured plan and validate it against the grounding pack in
        code. On plan-validation failure we don't hard-stop — the generator can
        still recover — but we attach the issue to the plan's assumptions so it
        surfaces to the reviewer."""
        system = (
            "You are the planning stage of a data-code generator. Given the "
            "user's request and the confirmed catalog context, output ONLY a "
            "JSON object describing the plan you will implement. Do not write "
            "any SQL or code yet. Fields: intent (string), tables (list of FQNs "
            "you will use, drawn only from the provided context), joins (list of "
            "{left,right,type}), grain (string), filters (list), output_columns "
            "(list of {name,type}), assumptions (list of strings), "
            "external_systems (list), side_effects (list like 'produce:topic' or "
            "'write:/path'). Use only identifiers present in the context."
        )
        user = (
            f"Request:\n{prompt}\n\n"
            f"Confirmed context:\n{grounding.render()}\n"
        )
        if repair is not None:
            user += (
                f"\nThe previous attempt failed at planning with: "
                f"{repair.message}. Revise the plan to fix this."
            )
        try:
            comp = await self.d.model.complete(
                model=model_name, system=system,
                messages=[{"role": "user", "content": user}],
                temperature=self.s.codegen_temp_plan, json_mode=True,
                max_tokens=1024,
            )
            self._acc(comp)
            raw = extract_json(comp.text)
            raw.setdefault("target", target)
            raw.setdefault("intent", prompt[:200])
            plan = Plan.model_validate(raw)
        except Exception as e:  # planning is best-effort; never fail the turn here
            log.warning("plan_stage_soft_fail", error=str(e))
            return Plan(target=target, intent=prompt[:200])

        # Validate the plan against the pack (§5.4) — attach issues as assumptions.
        known_tables = {t.fqn for t in grounding.tables}
        for tref in plan.tables:
            if known_tables and tref not in known_tables:
                plan.assumptions.append(
                    f"NOTE: planned table '{tref}' was not in confirmed context; verify."
                )
        if target in ("sql", "dbt") and not plan.grain:
            plan.assumptions.append("NOTE: output grain was not declared explicitly.")
        return plan

    async def _generate(
        self,
        *,
        target: Target,
        prompt: str,
        grounding: GroundingPack,
        plan: Plan | None,
        current_file_content: str | None,
        model_name: str,
        repair: Diagnostic | None,
        attempt: int,
    ) -> dict[str, Any]:
        """Produce a candidate artifact. Reuses the backend's per-target system
        prompt (dialect rules, house conventions) and layers the grounding pack,
        plan, and — on repair — the normalised diagnosis (not the whole prompt,
        §9.2) so the model fixes the specific problem."""
        base_system = self.d.backend.system_prompt(
            engine=self.d.engine, project_context=self.d.project_context
        )
        system = base_system + _GEN_SUFFIX

        parts = [f"Request:\n{prompt}"]
        parts.append(f"\nConfirmed context (use only these identifiers):\n{grounding.render()}")
        if plan is not None:
            parts.append(f"\nAgreed plan:\n{plan.model_dump_json(indent=2)}")
        if current_file_content:
            parts.append(
                "\n--- Current file content (propose the full edited version) ---\n"
                f"{current_file_content}"
            )
        if repair is not None:
            # feed back diagnosis + targeted context only (§9.2)
            ctx = ""
            if repair.suggested_context:
                ctx = "\nRelevant context: " + ", ".join(repair.suggested_context)
            parts.append(
                f"\nYour previous attempt failed validation:\n"
                f"[{repair.error_class.value}] {repair.message}{ctx}\n"
                "Fix exactly this problem; keep everything else that was correct."
            )

        temp = self.s.codegen_temp_repair if repair is not None else self.s.codegen_temp_generate
        comp = await self.d.model.complete(
            model=model_name, system=system,
            messages=[{"role": "user", "content": "\n".join(parts)}],
            temperature=temp,
        )
        self._acc(comp)

        code = extract_code(comp.text)
        # Path: dbt/flink models carry a path; ad hoc sql/python insert into the
        # open editor (path resolved by the caller/apply flow). Prefer plan side
        # effects for a suggested path when present.
        path = _suggest_path(target, plan, code)
        return {
            "text": comp.text,
            "code": code,
            "path": path,
            "companion": None,      # dbt schema.yml wired via backend.compile_code detail if needed
            "companion_path": None,
        }

    async def _validate(
        self,
        *,
        target: Target,
        code: str,
        path: str | None,
        companion: str | None,
        grounding: GroundingPack,
    ) -> ValidationReport:
        """Run the ladder. Python uses the static-only ladder here in-process
        (§7.5); sql/dbt/flink delegate to the backend's compile_code, which
        calls the lakehouse/dbt/flink validators, then we normalise the result."""
        if target == "python":
            return validate_python_notebook(code=code, path=path or "cell.py", grounding=grounding)

        # sql / dbt / flinksql -> backend compile (EXPLAIN / dbt validate / flink validate)
        t0 = time.perf_counter()
        result = await self.d.backend.compile_code(
            code=code, path=path or "query.sql", companion_code=companion, engine=self.d.engine
        )
        dur = int((time.perf_counter() - t0) * 1000)

        if result.get("skipped"):
            return ValidationReport(
                passed=True,
                rungs=[RungResult(rung="L2", status="skip", duration_ms=dur, detail=result)],
            )
        if result.get("ok"):
            return ValidationReport(
                passed=True,
                rungs=[RungResult(rung="L2", status="pass", duration_ms=dur, detail=result)],
            )

        # normalise the first error into a routed diagnostic
        from app.codegen_agent.diagnostics import normalise_flink_error, normalise_sql_error

        errors = result.get("errors") or ["validation failed"]
        raw = errors[0] if isinstance(errors, list) else str(errors)
        diag = (
            normalise_flink_error(str(raw))
            if target == "flinksql"
            else normalise_sql_error(str(raw))
        )
        return ValidationReport(
            passed=False,
            rungs=[RungResult(rung="L2", status="fail", duration_ms=dur,
                              detail=result, diagnostic=diag)],
            first_failure=diag,
        )

    # ------------------------------------------------------------------
    async def _publish_validation(self, report: ValidationReport, attempt: int) -> None:
        await self.d.publish({
            "type": "codegen.tool_call",  # reuse existing envelope type for UI compat
            "tool": "validate",
            "attempt": attempt,
            "result_ok": report.passed,
            "validation_status": report.status,
            "detail": report.as_detail(),
        })

    def _acc(self, comp) -> None:
        self._in += comp.input_tokens
        self._out += comp.output_tokens
        if comp.model:
            self._model_used = comp.model


# --------------------------------------------------------------------------
# Prompt fragments + helpers
# --------------------------------------------------------------------------
_GEN_SUFFIX = (
    "\n\nReturn the artifact as exactly one fenced code block. Use only tables "
    "and columns present in the confirmed context — never invent a schema. "
    "After the code, end with one short sentence asking the user to review it "
    'and click "Insert into editor" if it looks right (or tell you what to '
    "change)."
)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _guess_external_systems(prompt: str, target: Target) -> list[str]:
    """Very cheap hint for the grounding stage about what a python notebook
    might touch, so it knows whether to hit the schema registry. Not
    authoritative — grounding does the real resolution."""
    if target != "python":
        return []
    p = prompt.lower()
    hints: list[str] = []
    if "kafka" in p or "topic" in p or "produce" in p or "consume" in p:
        hints.append("kafka")
    if "parquet" in p or "csv" in p or "file" in p:
        hints.append("file")
    return hints


def _suggest_path(target: Target, plan: Plan | None, code: str | None) -> str | None:
    if target == "dbt":
        return "models/staging/model.sql"
    if target == "flinksql":
        return "flink/job.sql"
    # ad hoc sql / python: no inherent path — inserted into the open editor.
    return None


def _terminal_note(diag: Diagnostic) -> str:
    if diag.error_class is ErrorClass.POLICY_PII:
        return (
            "I stopped because the request would expose PII-tagged columns in the "
            "output without masking. Adjust the request to exclude or mask them."
        )
    if diag.error_class is ErrorClass.POLICY_WRITE_TARGET:
        return (
            "I stopped because the generated code targets a write location that "
            "isn't on the allowlist. This needs an elevated, reviewed flow."
        )
    return f"I stopped without auto-repairing: {diag.message}"


def _compose_reply(
    final_text: str,
    report: ValidationReport,
    repair_note: str | None,
    last_diag: Diagnostic | None,
) -> str:
    """Assemble the user-facing reply. On success, use the model's own text.
    On terminal/exhaustion, lead with the diagnosis (never hand over silently
    broken code, §9.2)."""
    if report.passed:
        return final_text
    lines = []
    if repair_note:
        lines.append(repair_note)
    elif last_diag:
        lines.append(f"I couldn't validate the code: {last_diag.message}")
    if last_diag and last_diag.offending_identifier:
        lines.append(f"Problem identifier: {last_diag.offending_identifier}")
    lines.append(
        "The last candidate is included below for reference, but it did not "
        "pass validation — review carefully before using it."
    )
    if final_text:
        lines.append("\n" + final_text)
    return "\n".join(lines)
