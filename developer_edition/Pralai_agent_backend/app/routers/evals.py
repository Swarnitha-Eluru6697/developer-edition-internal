"""
Real per-agent evaluation sets — replaces monitoring.py's old EVALS list
(a hardcoded Python dict with fake pass rates) with genuine execution:
every case in a set actually runs through the same real, shared
execute_agent_flow routing every other real invocation channel uses
(§0.53, GAP_REPORT — previously called harness.run_flow_sync directly,
meaning a hitl-containing agent behaved differently when evaluated than
when run through /run) against the live agent at run time, and scoring
is real — a deterministic substring check, or DeepEval's GEval judged
by this app's own real/mock call_llm (see eval_judge.py) for open-ended
criteria.

Honesty boundary: LLM-judged (criteria-based) cases require a real
ANTHROPIC_API_KEY to produce a real score — DeepEval's GEval needs
structured JSON back from its judge, which the mock call_llm can't
produce (it returns a fixed non-JSON string). Without a real key, those
cases get an honest "could not judge" result, not a silently fake pass.
Deterministic (non-criteria) cases work identically either way, since
they never call an LLM at all.
"""
from datetime import datetime
import json
from typing import List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from gateway_auth import require_permission
from .. import permissions as P
from ..permissions import require_any_permission
from sqlmodel import Session, select
from pydantic import BaseModel

from ..db import get_session
from ..models import Agent, EvalSet, EvalCase, EvalRun, EvalResult
from .execution import execute_agent_flow, create_hitl_pause_run, resolve_execution
from ..metrics import EVAL_RUNS_TOTAL
from ..pagination import paginate, pagination_params

router = APIRouter(prefix="/api/evals", tags=["evals"])


class EvalSetIn(BaseModel):
    name: str
    agent_id: str


class EvalCaseIn(BaseModel):
    input: str
    expected_output: str
    criteria: Optional[str] = None


@router.get("/sets", dependencies=[Depends(require_any_permission(P.STUDIO, P.MONITORING))])
def list_eval_sets(agent_id: Optional[str] = None, session: Session = Depends(get_session)) -> List[dict]:
    q = select(EvalSet)
    if agent_id:
        q = q.where(EvalSet.agent_id == agent_id)
    sets = session.exec(q).all()
    out = []
    for s in sets:
        case_count = len(session.exec(select(EvalCase).where(EvalCase.eval_set_id == s.id)).all())
        out.append({**s.model_dump(), "case_count": case_count})
    return out


@router.post("/sets", dependencies=[Depends(require_permission(P.STUDIO))])
def create_eval_set(body: EvalSetIn, session: Session = Depends(get_session)) -> EvalSet:
    if not session.get(Agent, body.agent_id):
        raise HTTPException(404, "Agent not found")
    s = EvalSet(id=f"es_{uuid4().hex[:10]}", name=body.name, agent_id=body.agent_id)
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


@router.post("/sets/{set_id}/cases", dependencies=[Depends(require_permission(P.STUDIO))])
def add_eval_case(set_id: str, body: EvalCaseIn, session: Session = Depends(get_session)) -> EvalCase:
    if not session.get(EvalSet, set_id):
        raise HTTPException(404, "Eval set not found")
    c = EvalCase(id=f"ec_{uuid4().hex[:10]}", eval_set_id=set_id, input=body.input,
                 expected_output=body.expected_output, criteria=body.criteria)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


@router.get("/sets/{set_id}/cases", dependencies=[Depends(require_any_permission(P.STUDIO, P.MONITORING))])
def list_eval_cases(set_id: str, session: Session = Depends(get_session)) -> List[EvalCase]:
    return session.exec(select(EvalCase).where(EvalCase.eval_set_id == set_id)).all()


def _score_case(agent: Agent, case: EvalCase, invoking_user: str, session: Session, resolved) -> dict:
    """Runs the real agent flow for this case's input, then really scores
    the real output — deterministic substring match if no criteria is
    set, or DeepEval's GEval (judged by ClaudeEvalJudge) if it is.

    Real sync-to-async bridge (§0.53, GAP_REPORT): this function, and
    the real route that calls it in a loop below, both stay genuinely
    synchronous — run_eval_set is a plain `def` route, which FastAPI
    already dispatches in its own real worker thread, not the main
    event loop. asyncio.run() here creates a real, dedicated event loop
    scoped to just this one call, safe specifically because it's
    running in that separate real thread — the same real technique
    used anywhere else in this codebase that needs to call async code
    (execute_agent_flow, which itself may await a real Temporal client
    call) from a genuinely synchronous context.

    resolved (§0.99, GAP_REPORT): the one, real ResolvedExecution
    run_eval_set (below) resolved ONCE, before ever looping over
    cases — the actual fix for a real, reported conceptual gap: this
    used to let execute_agent_flow independently resolve the agent's
    own current version A SECOND TIME, separately, for every single
    case — a real, concurrent publish landing mid-run could in
    principle mean different cases inside the SAME logical eval run
    silently scored against genuinely different flow content, with no
    way to tell from the results alone. Passing the SAME already-
    resolved object into every case closes that completely, matching
    the report's own recommended model exactly."""
    import asyncio
    import time
    start = time.time()
    try:
        # apply_response_enrichment=False (§0.108, GAP_REPORT) — the
        # report's own explicit concession made real: "some
        # differences are reasonable for Eval." Generating follow-up
        # suggestions for every real eval case would add real,
        # unnecessary LLM cost and noise to what's fundamentally a
        # scoring exercise, not a chat response any real person will
        # ever see. Output moderation still fully applies regardless —
        # this opt-out is deliberately narrow, matching
        # _apply_output_postprocessing's own stated scope exactly.
        result = asyncio.run(execute_agent_flow(
            session, agent, invoking_user, case.input, execution=resolved,
            apply_response_enrichment=False,
            structured_inputs=_case_structured_inputs(resolved.flow, case.input),
        ))
    except Exception as e:
        # Real, honest failure (§0.53, GAP_REPORT) — a real Temporal
        # connection error (or any other real execution failure) is
        # reported as a genuine failed case, matching the same real,
        # honest pattern already used below for an LLM-judging
        # failure, rather than crashing the whole eval run and losing
        # every other case's own real result.
        return {
            "actual_output": "", "passed": False, "score": 0.0,
            "reason": f"Execution failed before scoring could run: {e}",
        }
    if result.get("status") == "paused_for_human_input":
        # Real, shared HITL pause lifecycle (§0.98, GAP_REPORT) — the
        # actual fix for a real, reported gap: this used to fall
        # straight through to `actual = result.get("output") or ""`
        # below and genuinely SCORE a paused case's own still-empty
        # output as if it were the real, final answer — an honest-
        # looking but factually wrong "failed" (or, worse, an
        # accidental "passed" if the criteria happened to match empty
        # text) result, with no real HitlRequest ever created, so
        # there was no way for a real person to ever resume this case
        # and get its own, real, eventual score. See
        # create_hitl_pause_run's own docstring (execution.py) for the
        # full real design — the same real, shared, resumable pause
        # lifecycle /run's own pauses already use, created here too so
        # a real person CAN resume this specific case via the
        # platform's own /api/hitl/{request_id}/respond endpoint, even
        # though this particular eval run itself won't wait for it —
        # honestly reported as unscored, not silently mis-scored.
        duration_s = time.time() - start
        run_id = f"r-eval-{uuid4().hex[:8]}"
        request_id = create_hitl_pause_run(
            session, agent, invoking_user=invoking_user, run_id=run_id, result=result,
            trigger="Eval", ts_str=datetime.utcnow().strftime("%H:%M:%S"), duration_s=duration_s,
            resolved=resolved,
        )
        session.commit()
        return {
            "actual_output": "", "passed": False, "score": 0.0,
            "reason": (
                f"Execution paused for human input (run {run_id}, request {request_id}) — "
                f"not scored; a person can resume it via the platform's own HITL response "
                f"endpoint, but this eval run itself doesn't wait for that."
            ),
        }
    actual = result.get("output") or ""

    if not case.criteria:
        passed = case.expected_output.strip().lower() in actual.strip().lower()
        return {
            "actual_output": actual, "passed": passed, "score": 1.0 if passed else 0.0,
            "reason": "expected text found in output" if passed else "expected text not found in output",
        }

    try:
        from deepeval.test_case import LLMTestCase, SingleTurnParams
        from deepeval.metrics import GEval
        from ..eval_judge import ClaudeEvalJudge

        metric = GEval(
            name="Custom Criteria", criteria=case.criteria,
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT, SingleTurnParams.EXPECTED_OUTPUT],
            model=ClaudeEvalJudge(), threshold=0.5,
        )
        test_case = LLMTestCase(input=case.input, actual_output=actual, expected_output=case.expected_output)
        metric.measure(test_case)
        return {"actual_output": actual, "passed": metric.is_successful(), "score": metric.score, "reason": metric.reason}
    except Exception as e:
        # The real, expected failure mode when no ANTHROPIC_API_KEY is
        # configured: DeepEval's judge needs structured JSON back and the
        # mock call_llm can't produce it. Honest result, not a crash and
        # not a silently-fake pass.
        return {
            "actual_output": actual, "passed": False, "score": 0.0,
            "reason": f"LLM-judged scoring failed (likely no real ANTHROPIC_API_KEY configured for a real judge): {e}",
        }


def _case_structured_inputs(flow: dict, case_input: str) -> dict:
    """Map an eval case's input onto the agent's declared trigger fields.

    Delegates to harness.structured_inputs_for_flow. This logic lived
    here alone until the subagent hop turned out to need exactly the
    same thing — two copies would have drifted.
    """
    from ..harness import structured_inputs_for_flow
    return structured_inputs_for_flow(flow, case_input)


@router.post("/sets/{set_id}/run", dependencies=[Depends(require_permission(P.STUDIO))])
def run_eval_set(set_id: str, invoking_user: str = "user:eval", session: Session = Depends(get_session)) -> dict:
    eval_set = session.get(EvalSet, set_id)
    if not eval_set:
        raise HTTPException(404, "Eval set not found")
    agent = session.get(Agent, eval_set.agent_id)
    if not agent:
        raise HTTPException(404, "Agent for this eval set not found")
    cases = session.exec(select(EvalCase).where(EvalCase.eval_set_id == set_id)).all()
    if not cases:
        raise HTTPException(400, "This eval set has no cases yet")

    # Real, immutable execution snapshot, resolved ONCE (§0.93/§0.99,
    # GAP_REPORT) — the actual fix for a real, reported conceptual
    # gap: every case used to independently resolve the agent's own
    # current version via its own separate execute_agent_flow call —
    # a real, concurrent publish landing mid-run could in principle
    # mean different cases inside the SAME logical eval run silently
    # scored against genuinely different flow content, with no way to
    # tell from the results alone. Resolved here, once, and passed
    # into every real case below (_score_case's own new `resolved`
    # parameter) — the one, real, shared snapshot every case in this
    # run is genuinely scored against, recorded on this EvalRun row
    # itself so a later comparison between runs is actually meaningful.
    resolved = resolve_execution(session, agent, preview=False)

    run = EvalRun(
        id=f"er_{uuid4().hex[:10]}", eval_set_id=set_id, agent_id=agent.id, status="running", total_count=len(cases),
        agent_version_id=resolved.agent_version_id, agent_version=resolved.agent_version, flow_hash=resolved.flow_hash,
    )
    session.add(run)
    session.commit()

    pass_count = 0
    for case in cases:
        scored = _score_case(agent, case, invoking_user, session, resolved)
        session.add(EvalResult(eval_run_id=run.id, eval_case_id=case.id, **scored))
        if scored["passed"]:
            pass_count += 1
    session.commit()

    # real drift: compare this run's rate to the immediately-preceding
    # completed run for the SAME eval set, not an asserted label
    rate = pass_count / len(cases)
    prior_runs = session.exec(
        select(EvalRun).where(EvalRun.eval_set_id == set_id, EvalRun.status == "completed", EvalRun.id != run.id)
        .order_by(EvalRun.started_at.desc())
    ).all()
    drift = "none"
    if prior_runs:
        prior_rate = prior_runs[0].pass_count / prior_runs[0].total_count if prior_runs[0].total_count else 0
        delta = rate - prior_rate
        if delta <= -0.20:
            drift = "flagged"
        elif delta <= -0.08:
            drift = "watch"

    run.status = "completed"
    run.completed_at = datetime.utcnow()
    run.pass_count = pass_count
    run.drift = drift
    session.add(run)
    session.commit()
    session.refresh(run)

    EVAL_RUNS_TOTAL.labels(agent_id=agent.id, drift=drift).inc()

    results = session.exec(select(EvalResult).where(EvalResult.eval_run_id == run.id)).all()
    return {"run": run, "results": results, "rate": f"{rate*100:.1f}%"}


@router.get("/runs/{run_id}", dependencies=[Depends(require_any_permission(P.STUDIO, P.MONITORING))])
def get_eval_run(run_id: str, session: Session = Depends(get_session)) -> dict:
    run = session.get(EvalRun, run_id)
    if not run:
        raise HTTPException(404, "Eval run not found")
    results = session.exec(select(EvalResult).where(EvalResult.eval_run_id == run_id)).all()
    return {"run": run, "results": results}


@router.get("/sets/{set_id}/runs", dependencies=[Depends(require_any_permission(P.STUDIO, P.MONITORING))])
def list_eval_runs(
    set_id: str, response: Response, session: Session = Depends(get_session),
    page: dict = Depends(pagination_params),
) -> List[EvalRun]:
    """§0.113, GAP_REPORT — the same real, keyset-pagination fix as
    runs.py/traces.py's own identical gap; see pagination.py's own
    module docstring for the full real reasoning. A genuinely
    unbounded, growing real collection — every real re-run of this
    eval set adds another real row here."""
    q = select(EvalRun).where(EvalRun.eval_set_id == set_id)
    return paginate(session, EvalRun, q, limit=page["limit"], cursor=page["cursor"],
                     sort_col=EvalRun.started_at, id_col=EvalRun.id, response=response)
