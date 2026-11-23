#!/usr/bin/env python3
"""
The real input contract of a trained artifact: the exact columns its saved
preprocessor was fitted on, in order.

Input: JSON on stdin, {"modelFilePath": "..."}. Output: one JSON line.

Reads the same `feature_names_in_` the prediction path enforces, so the schema
shown to a caller and the schema they are judged against cannot disagree.
Handles both artifact shapes: a plain joblib bundle, and the deep-learning
tar.gz — for which only the preprocessor member is opened, so this stays cheap
and never imports torch.
"""
import sys
import json
import os
import tarfile
import tempfile
import shutil


def _feature_names(bundle):
    pre = bundle.get("preprocessor") if isinstance(bundle, dict) else None
    if pre is None:
        return None, "artifact has no saved preprocessor, so it carries no named column contract"
    # feature_names_in_ is a numpy array — `or []` on one raises "truth value
    # of an array is ambiguous", so it is checked for None explicitly.
    raw = getattr(pre, "feature_names_in_", None)
    names = list(raw) if raw is not None else []
    if not names:
        return None, "the saved preprocessor records no input feature names"
    return [str(n) for n in names], None


def main():
    try:
        req = json.loads(sys.stdin.read())
    except Exception as e:
        print(json.dumps({"available": False, "reason": f"invalid input: {e}"}))
        return
    path = req.get("modelFilePath") or ""
    try:
        import joblib
    except Exception as e:
        print(json.dumps({"available": False, "reason": f"joblib not available: {e}"}))
        return

    workdir = None
    try:
        if path.endswith(".tar.gz"):
            workdir = tempfile.mkdtemp(prefix="ml-studio-schema-")
            with tarfile.open(path, "r:gz") as tar:
                member = next((m for m in tar.getmembers() if m.name == "preprocessor.joblib"), None)
                if member is None:
                    print(json.dumps({"available": False, "reason": "artifact bundle has no preprocessor.joblib"}))
                    return
                tar.extract(member, workdir)
            bundle = joblib.load(os.path.join(workdir, "preprocessor.joblib"))
        else:
            bundle = joblib.load(path)
    except Exception as e:
        print(json.dumps({"available": False, "reason": f"couldn't read the artifact: {e}"}))
        return
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    names, reason = _feature_names(bundle)
    if names is None:
        print(json.dumps({"available": False, "reason": reason}))
        return
    print(json.dumps({"available": True, "columns": names}))


if __name__ == "__main__":
    main()
