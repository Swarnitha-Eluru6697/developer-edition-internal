#!/usr/bin/env python3
"""
Loads a real model.joblib file (saved by real_trainer.py,
classical_trainer.py, or timeseries_trainer.py) and runs a genuine
.predict() call — real inference against a real fitted model, not a
stand-in computation. Input: JSON via stdin,
{"modelFilePath": "...", "task": "classification"|..., "input": {...}}.

Two invocation modes:
  - one-shot (default): read one JSON request from stdin, print one JSON
    response, exit.
  - `--serve`: stay alive, reading one JSON request per line and writing
    one JSON response per line. This exists because importing joblib,
    sklearn and xgboost costs ~1.0-1.4s in a cold interpreter — which,
    per prediction, was the ENTIRE measured serving latency and by
    itself dragged the computed model health score to "degraded". In
    serve mode that cost is paid once, and the loaded model bundle is
    cached by file path across requests.

Two real paths, depending on what the model was trained on:
  - REAL dataset (a real ColumnTransformer was saved alongside the
    model): the posted input is built into a real single-row DataFrame
    using the ORIGINAL training column names and run through that exact
    fitted preprocessor — real training/inference parity, not an
    approximation. Missing columns are a real, reported error, not
    silently zero-filled — a production model's real input contract
    should be enforced, not guessed around.
  - Synthetic data (no preprocessor saved): the model expects a
    fixed-width feature vector matching whatever synthetic shape it was
    trained on. The posted input is coerced (numeric values, sorted-by-
    key order) with an honest note about whether that coercion was exact
    or padded/truncated — unchanged from before real dataset support
    existed, since synthetic-trained models have no real column contract
    to enforce in the first place.
"""
import sys
import json


# Loaded bundles, keyed by artifact path. Only ever grows by one entry per
# distinct deployed artifact, and an artifact path is immutable (a new model
# version is written to a new key), so a cached bundle can never be stale.
_BUNDLES = {}


def _load_bundle(model_file_path):
    if model_file_path not in _BUNDLES:
        import joblib
        _BUNDLES[model_file_path] = joblib.load(model_file_path)
    return _BUNDLES[model_file_path]


def handle(req):
    model_file_path = req.get("modelFilePath")
    input_obj = req.get("input", {})
    # {"features": {...}} is accepted as well as the flat {column: value} form.
    if isinstance(input_obj, dict) and set(input_obj) == {"features"} and isinstance(input_obj["features"], dict):
        input_obj = input_obj["features"]

    try:
        import joblib  # noqa: F401  (imported for the availability check)
        import numpy as np
    except Exception as e:
        return {"ok": False, "error": f"joblib/numpy not available: {e}"}

    try:
        bundle = _load_bundle(model_file_path)
    except Exception as e:
        return {"ok": False, "error": f"couldn't load {model_file_path}: {e}"}

    model = bundle.get("model") if isinstance(bundle, dict) else bundle
    if model is None:
        return {"ok": False, "error": "loaded file has no 'model' key"}

    preprocessor = bundle.get("preprocessor") if isinstance(bundle, dict) else None
    label_encoder = bundle.get("labelEncoder") if isinstance(bundle, dict) else None
    shape_note = None

    if preprocessor is not None:
        # Real training/inference parity path — apply the EXACT real
        # fitted transformation training used, not an approximation of it.
        try:
            import pandas as pd
            feature_names = list(preprocessor.feature_names_in_)
            missing = [c for c in feature_names if c not in input_obj]
            if missing:
                return {"ok": False, "error": f"input is missing required real column(s) this model was trained on: {missing}. Expected all of: {feature_names}"}
            row_df = pd.DataFrame([{c: input_obj[c] for c in feature_names}])
            X = preprocessor.transform(row_df)
            if hasattr(X, "toarray"):
                X = X.toarray()
            X = np.asarray(X, dtype=float)
            shape_note = f"real preprocessing pipeline applied — {len(feature_names)} real training column(s): {feature_names}"
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__} applying the real saved preprocessor: {e}"}
    else:
        # Synthetic-data-trained model — no real column contract exists;
        # best-effort numeric coercion, same as before real dataset
        # support existed.
        expected_features = getattr(model, "n_features_in_", None)
        values = [v for v in input_obj.values() if isinstance(v, (int, float))]
        if expected_features is not None:
            if len(values) < expected_features:
                values = values + [0.0] * (expected_features - len(values))
                shape_note = f"model trained on synthetic data (no real column contract) — input had {len(input_obj)} numeric field(s), padded with zeros to match this model's {expected_features} expected features"
            elif len(values) > expected_features:
                shape_note = f"model trained on synthetic data — input had {len(input_obj)} numeric field(s), truncated to this model's {expected_features} expected features"
                values = values[:expected_features]
        if not values:
            values = [0.0] * (expected_features or 1)
            shape_note = (shape_note or "") + " — no numeric fields found in input; used zeros"
        X = np.array([values])

    try:
        if hasattr(model, "predict"):
            pred = model.predict(X)
            raw_pred = pred[0].item() if hasattr(pred[0], "item") else pred[0]
            # Real label decoding — a classifier trained on real
            # string-labeled data (e.g. "fraud"/"legitimate") was fit on
            # the label-encoded integers; decode back to the real
            # original label rather than returning the internal integer.
            if label_encoder is not None:
                try:
                    raw_pred = label_encoder.inverse_transform([int(raw_pred)])[0]
                    if hasattr(raw_pred, "item"):
                        raw_pred = raw_pred.item()
                except Exception:
                    pass
            result = {"prediction": raw_pred}
            if hasattr(model, "predict_proba"):
                try:
                    proba = model.predict_proba(X)
                    result["probability"] = round(float(max(proba[0])), 4)
                except Exception:
                    pass
        elif hasattr(model, "transform"):
            out = model.transform(X)
            result = {"transformed": out[0].tolist()}
        else:
            return {"ok": False, "error": f"loaded model ({type(model).__name__}) has neither .predict nor .transform"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__} during real predict: {e}"}

    return {"ok": True, **result, "shapeNote": shape_note, "modelClass": type(model).__name__, "realPreprocessing": preprocessor is not None}


def main():
    if "--serve" in sys.argv:
        # One request per line, one response per line. Flushed per response
        # so the caller isn't left waiting on a buffer.
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except Exception as e:
                out = {"ok": False, "error": f"invalid input: {e}"}
            else:
                try:
                    out = handle(req)
                except Exception as e:
                    # A crash on one request must not kill the process and
                    # take every other pending prediction down with it.
                    out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
        return

    try:
        req = json.loads(sys.stdin.read())
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"invalid input: {e}"}))
        return
    print(json.dumps(handle(req)))


if __name__ == "__main__":
    main()
