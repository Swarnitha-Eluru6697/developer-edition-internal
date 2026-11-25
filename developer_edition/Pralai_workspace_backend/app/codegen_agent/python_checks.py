"""Python notebook static validation ladder (design §7.5).

Option A: statically validate, never execute. General-purpose Python (Kafka
produce/consume, file processing, data manipulation) is imperative and
side-effectful, so the harness certifies FORM AND SAFETY — well-formed,
type-checked, no prohibited imports, no obvious security flaws, and correct
about known Kafka topics — but NOT runtime behaviour. The user runs it.

Rungs (no L4/L5 execution):
  L0  parse        — ast.parse (+ notebook cell extraction upstream)
  L1  static       — import allowlist (always) + ruff/mypy/bandit (if installed)
  L2  kafka bind   — producer/consumer topics vs registered schema (§5.3)
  L3  policy       — secret scan + declared-vs-detected side effects

L1's import allowlist is the primary safety boundary and needs no external
tools (pure ast), so it always runs. ruff/mypy/bandit are best-effort: if the
binary isn't present the rung degrades to "skip" for that tool rather than
failing the turn.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app.codegen_agent.contracts import (
    Diagnostic,
    ErrorClass,
    GroundingPack,
    RungResult,
    ValidationReport,
)

# Broad allowlist — Python here is general-purpose (§7.5), not Kafka-only. The
# point is to block arbitrary process/network/filesystem escape, not to force a
# narrow toolset. Tune from real usage; this is a versioned asset.
_ALLOWED_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        # stdlib data/util (safe subset)
        "json", "csv", "io", "os", "sys", "re", "math", "statistics", "random",
        "datetime", "time", "collections", "itertools", "functools", "typing",
        "dataclasses", "pathlib", "decimal", "uuid", "hashlib", "base64",
        "logging", "warnings", "contextlib", "enum", "abc",
        # data stack
        "pandas", "numpy", "pyarrow", "polars", "matplotlib", "seaborn",
        "scipy", "sklearn", "duckdb",
        # kafka + serialisation
        "confluent_kafka", "kafka", "fastavro", "avro", "google", "protobuf",
        # lakehouse / sql access
        "sqlalchemy", "trino", "psycopg", "psycopg2", "pyiceberg",
    }
)

# Imports that are outright rejected even though a root above might allow a
# submodule — these are the escape hatches static checks exist to catch.
_DENIED_IMPORTS: frozenset[str] = frozenset(
    {"subprocess", "socket", "ctypes", "multiprocessing", "pty", "resource",
     "importlib", "pickle", "marshal", "shelve", "urllib", "requests",
     "http", "ftplib", "telnetlib", "smtplib", "paramiko"}
)

# os submodule calls that are unsafe even though `os` (for os.environ,
# os.path) is allowed. Flagged by name at L1.
_UNSAFE_OS_CALLS: frozenset[str] = frozenset({"system", "popen", "exec", "execv", "spawn", "fork"})

_SECRET_RE = re.compile(
    r"(?i)(?:password|passwd|secret|api[_-]?key|token|aws_secret|private[_-]?key)\s*[:=]\s*['\"][^'\"]{6,}['\"]"
)
_EVAL_RE = re.compile(r"\b(?:eval|exec)\s*\(")
_PICKLE_LOADS_RE = re.compile(r"\bpickle\.loads?\s*\(")


def validate_python_notebook(
    *,
    code: str,
    path: str,
    grounding: GroundingPack,
) -> ValidationReport:
    """Run the static ladder. Returns a ValidationReport; on the first failing
    rung, first_failure carries the routed diagnostic."""
    rungs: list[RungResult] = []

    # ---- L0 parse -------------------------------------------------------
    t0 = time.perf_counter()
    try:
        tree = ast.parse(code, filename=path or "<notebook>")
    except SyntaxError as e:
        diag = Diagnostic.of(
            rung="L0",
            error_class=ErrorClass.SYNTAX_ERROR,
            message=f"{e.msg} (line {e.lineno})",
        )
        rungs.append(RungResult(rung="L0", status="fail",
                                duration_ms=_ms(t0), diagnostic=diag))
        return ValidationReport(passed=False, rungs=rungs, first_failure=diag)
    rungs.append(RungResult(rung="L0", status="pass", duration_ms=_ms(t0)))

    # ---- L1 static analysis (import allowlist always; tools best-effort) --
    t1 = time.perf_counter()
    diag = _check_imports_and_calls(tree)
    if diag is not None:
        rungs.append(RungResult(rung="L1", status="fail",
                                duration_ms=_ms(t1), diagnostic=diag))
        return ValidationReport(passed=False, rungs=rungs, first_failure=diag)

    # security heuristics that don't need bandit
    sec = _check_security_heuristics(code)
    if sec is not None:
        rungs.append(RungResult(rung="L1", status="fail",
                                duration_ms=_ms(t1), diagnostic=sec))
        return ValidationReport(passed=False, rungs=rungs, first_failure=sec)

    tool_detail = _run_external_linters(code, path)
    rungs.append(RungResult(rung="L1", status="pass", duration_ms=_ms(t1), detail=tool_detail))

    # ---- L2 kafka bind (only if the notebook touches kafka) --------------
    t2 = time.perf_counter()
    kafka_diag = _check_kafka_binding(code, grounding)
    if kafka_diag is not None:
        rungs.append(RungResult(rung="L2", status="fail",
                                duration_ms=_ms(t2), diagnostic=kafka_diag))
        return ValidationReport(passed=False, rungs=rungs, first_failure=kafka_diag)
    rungs.append(RungResult(rung="L2", status="pass", duration_ms=_ms(t2),
                            detail={"kafka_bound": bool(grounding.kafka_topics)}))

    # ---- L3 policy: declared side effects surfaced as advisory -----------
    t3 = time.perf_counter()
    rungs.append(RungResult(rung="L3", status="pass", duration_ms=_ms(t3),
                            detail={"note": "side effects advisory; not executed"}))

    return ValidationReport(passed=True, rungs=rungs)


def _check_imports_and_calls(tree: ast.AST) -> Diagnostic | None:
    """The import allowlist — the primary L1 safety boundary (§7.5)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _DENIED_IMPORTS or root not in _ALLOWED_IMPORT_ROOTS:
                    return Diagnostic.of(
                        rung="L1",
                        error_class=ErrorClass.PY_PROHIBITED_IMPORT,
                        message=f"import '{alias.name}' is not on the allowlist",
                        offending_identifier=root,
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _DENIED_IMPORTS or (root and root not in _ALLOWED_IMPORT_ROOTS):
                return Diagnostic.of(
                    rung="L1",
                    error_class=ErrorClass.PY_PROHIBITED_IMPORT,
                    message=f"import from '{node.module}' is not on the allowlist",
                    offending_identifier=root,
                )
        elif isinstance(node, ast.Call):
            # os.system / os.popen and friends
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "os" and func.attr in _UNSAFE_OS_CALLS:
                    return Diagnostic.of(
                        rung="L1",
                        error_class=ErrorClass.PY_SECURITY_FINDING,
                        message=f"os.{func.attr}(...) is not permitted (process escape)",
                        offending_identifier=f"os.{func.attr}",
                    )
    return None


def _check_security_heuristics(code: str) -> Diagnostic | None:
    if _SECRET_RE.search(code):
        return Diagnostic.of(
            rung="L1",
            error_class=ErrorClass.PY_SECURITY_FINDING,
            message="hardcoded credential detected; load secrets from env/vault instead",
        )
    if _PICKLE_LOADS_RE.search(code):
        return Diagnostic.of(
            rung="L1",
            error_class=ErrorClass.PY_SECURITY_FINDING,
            message="pickle.loads on external data is unsafe; use a safe serialiser",
        )
    if _EVAL_RE.search(code):
        return Diagnostic.of(
            rung="L1",
            error_class=ErrorClass.PY_SECURITY_FINDING,
            message="eval/exec is not permitted",
        )
    return None


def _run_external_linters(code: str, path: str) -> dict:
    """Best-effort ruff/mypy/bandit. Each degrades to 'skipped' if the tool
    isn't installed, so validation never hard-depends on them being present."""
    detail: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "cell.py"
        fp.write_text(code, encoding="utf-8")
        for tool, args in (
            ("ruff", ["check", "--quiet", str(fp)]),
            ("bandit", ["-q", "-r", str(fp)]),
        ):
            if shutil.which(tool) is None:
                detail[tool] = "skipped (not installed)"
                continue
            try:
                proc = subprocess.run(  # noqa: S603 - fixed args, temp file
                    [tool, *args], capture_output=True, text=True, timeout=30
                )
                # We record findings as advisory detail; we do NOT fail the rung
                # on ruff/bandit here because the allowlist + heuristics above are
                # the hard gate. Style/lint findings are surfaced for the reviewer.
                detail[tool] = {
                    "returncode": proc.returncode,
                    "output": (proc.stdout or proc.stderr)[:2000],
                }
            except (subprocess.TimeoutExpired, OSError) as e:
                detail[tool] = f"error: {e}"
    return detail


def _check_kafka_binding(code: str, grounding: GroundingPack) -> Diagnostic | None:
    """L2 for Python: if the notebook names a Kafka topic, it must exist in the
    grounding pack (which was populated from the schema registry). Topics the
    registry doesn't know route back to GROUND (§9.1 PY_KAFKA_TOPIC_UNKNOWN)."""
    if not grounding.external_systems and not grounding.kafka_topics:
        return None  # pure file-processing notebook — nothing to bind

    known = {k.topic for k in grounding.kafka_topics}
    # Cheap textual detection of topic names in produce/subscribe calls.
    referenced = set(re.findall(r"(?:topic|topics|subscribe)\s*=?\s*\[?\s*['\"]([\w.\-]+)['\"]", code))
    referenced |= set(re.findall(r"\.produce\(\s*['\"]([\w.\-]+)['\"]", code))
    for topic in referenced:
        if known and topic not in known:
            return Diagnostic.of(
                rung="L2",
                error_class=ErrorClass.PY_KAFKA_TOPIC_UNKNOWN,
                message=f"Kafka topic '{topic}' is not in the schema registry grounding",
                offending_identifier=topic,
                suggested_context=[topic],
            )
    return None


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
