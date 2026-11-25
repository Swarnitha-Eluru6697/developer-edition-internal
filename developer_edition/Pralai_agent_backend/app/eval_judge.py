"""
Wires DeepEval's LLM-as-judge metrics (GEval, etc.) to this app's own
call_llm — the same real Anthropic call every LLM/Agent node already
uses (a missing API key genuinely raises here now, same as everywhere
else — see model_adapter.py §0.58, GAP_REPORT). DeepEval defaults to
calling OpenAI for its judge model; this replaces that with our own real
integration rather than adding a second, separately-configured LLM
provider just for evals.
"""
import os
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")  # this sandbox's network doesn't allow DeepEval's own analytics egress; a real deployment may want this unset

from deepeval.models.base_model import DeepEvalBaseLLM

from .model_adapter import call_llm


class ClaudeEvalJudge(DeepEvalBaseLLM):
    """A judge model DeepEval's metrics call to grade an eval case. Every
    `generate()` call here is a REAL call to this app's own call_llm —
    since §0.58 (GAP_REPORT), a missing ANTHROPIC_API_KEY (without a
    real, explicit ALLOW_MOCK_MODELS=true opt-in) means this genuinely
    raises rather than silently returning a mock grade; the real
    caller (routers/evals.py's own _score_case) already catches this
    and reports an honest "could not judge" result, not a crash and
    not a silently-fake pass."""

    def __init__(self, model: str = "Claude Sonnet 5"):
        self._model_name = model
        super().__init__(model)

    def load_model(self):
        return self._model_name

    def generate(self, prompt: str) -> str:
        result = call_llm(prompt, model=self._model_name, system="You are grading an AI agent's output. Follow the instructions exactly.")
        return result["text"]

    async def a_generate(self, prompt: str) -> str:
        # call_llm is synchronous (real httpx/anthropic-sdk call under the
        # hood, same as everywhere else in this app) — no real async path
        # exists to await here, so this just calls the sync version.
        return self.generate(prompt)

    def get_model_name(self) -> str:
        return self._model_name
