"""Structured progress lines for ML Studio's training logs.

A trainer reports its result once, on stdout, when it finishes. Everything it does
on the way — data loaded, split, parameters, each optimiser step, metrics — goes
here instead: one `MLLOG {json}` line on stderr per event. The Node side
(services/runLog.ts) streams those lines into training_run_logs while the run is
still going, so the run's log page shows what is happening, not just how it ended.
"""
import json
import sys
import time

_T0 = time.time()


def log(msg, level="info", **data):
    rec = {"msg": str(msg), "level": level, "elapsed_s": round(time.time() - _T0, 2)}
    if data:
        rec["data"] = data
    sys.stderr.write("MLLOG " + json.dumps(rec, default=str) + "\n")
    sys.stderr.flush()


def describe_params(model, limit=14):
    """The estimator's effective parameters (defaults included), compactly."""
    try:
        p = model.get_params(deep=False)
    except Exception:
        return {}
    out = {}
    for k in sorted(p)[:limit]:
        v = p[k]
        out[k] = v if isinstance(v, (int, float, str, bool, type(None))) else type(v).__name__
    return out
