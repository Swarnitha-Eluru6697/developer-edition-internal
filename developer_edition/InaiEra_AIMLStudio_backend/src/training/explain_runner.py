#!/usr/bin/env python3
"""
Real feature attribution, read out of the actual fitted model.

Input: JSON on stdin, {"modelFilePath": "..."}.
Output: one JSON line.

This is genuine model introspection, not an approximation: tree ensembles
(XGBoost, LightGBM, RandomForest, GradientBoosting, CatBoost) expose
`feature_importances_` computed during their own fit, and linear models expose
`coef_`. Both are read directly off the loaded estimator.

The hard part is NAMES. The estimator only knows column positions after the
ColumnTransformer has expanded one-hot categories, so a raw importance vector
is a list of anonymous numbers. `get_feature_names_out()` on the saved
preprocessor maps them back to real training columns, and per-category
contributions are summed back onto the original column they came from — so
"merchant_category" appears once with its total importance rather than as six
separate one-hot columns.

Reports honestly rather than inventing: a model exposing neither attribute
(KMeans, PCA, a raw neural net) returns available=false with the real reason.
SHAP-style per-prediction attribution is NOT computed here — that needs a
background dataset and is a different, much more expensive thing; this is
global importance only and says so.
"""
import sys
import json


def _original_column(expanded_name, source_columns):
    """Map an expanded feature name back to the training column it came from.

    ColumnTransformer emits names like "c3__merchant_category_online" or
    "num__amount_usd" depending on how the transformer was named. Longest match
    wins so "amount" doesn't swallow "amount_usd".
    """
    best = None
    for col in source_columns:
        if col in expanded_name and (best is None or len(col) > len(best)):
            best = col
    return best or expanded_name


def main():
    try:
        req = json.loads(sys.stdin.read())
    except Exception as e:
        print(json.dumps({"available": False, "reason": f"invalid input: {e}"}))
        return

    path = req.get("modelFilePath")
    try:
        import joblib
        import numpy as np
    except Exception as e:
        print(json.dumps({"available": False, "reason": f"joblib/numpy not available: {e}"}))
        return

    try:
        bundle = joblib.load(path)
    except Exception as e:
        print(json.dumps({"available": False, "reason": f"couldn't load the model artifact: {e}"}))
        return

    model = bundle.get("model") if isinstance(bundle, dict) else bundle
    preprocessor = bundle.get("preprocessor") if isinstance(bundle, dict) else None
    if model is None:
        print(json.dumps({"available": False, "reason": "artifact has no 'model' key"}))
        return

    # --- the raw importance vector, straight off the fitted estimator --------
    kind = None
    values = None
    if hasattr(model, "feature_importances_"):
        values = np.asarray(model.feature_importances_, dtype=float)
        kind = "impurity/gain importance, computed by the model during fit"
    elif hasattr(model, "coef_"):
        coef = np.asarray(model.coef_, dtype=float)
        # Multi-class linear models carry one row per class; magnitude across
        # classes is the comparable per-feature quantity.
        values = np.abs(coef).mean(axis=0) if coef.ndim > 1 else np.abs(coef)
        kind = "absolute model coefficient (linear model)"
    else:
        print(json.dumps({
            "available": False,
            "reason": f"{type(model).__name__} exposes neither feature_importances_ nor coef_, so it has no global feature attribution to report",
        }))
        return

    if values is None or not len(values):
        print(json.dumps({"available": False, "reason": "the fitted model reported an empty importance vector"}))
        return

    # --- names ---------------------------------------------------------------
    expanded_names = None
    source_columns = []
    if preprocessor is not None:
        try:
            source_columns = list(getattr(preprocessor, "feature_names_in_", []))
            expanded_names = [str(n) for n in preprocessor.get_feature_names_out()]
        except Exception:
            expanded_names = None

    if expanded_names and len(expanded_names) == len(values):
        if source_columns:
            # Sum one-hot fragments back onto their original training column.
            totals = {}
            for name, v in zip(expanded_names, values):
                col = _original_column(name, source_columns)
                totals[col] = totals.get(col, 0.0) + float(v)
            pairs = list(totals.items())
            grouping = "per training column (one-hot categories summed back onto their source column)"
        else:
            pairs = [(n, float(v)) for n, v in zip(expanded_names, values)]
            grouping = "per expanded model feature"
    else:
        pairs = [(f"feature_{i}", float(v)) for i, v in enumerate(values)]
        grouping = "positional — the artifact carries no preprocessor, so real column names are unavailable"

    total = sum(abs(v) for _, v in pairs) or 1.0
    pairs.sort(key=lambda kv: -abs(kv[1]))
    features = [
        {"feature": name, "importance": round(float(v), 6), "sharePct": round(100.0 * abs(v) / total, 2)}
        for name, v in pairs
    ]

    print(json.dumps({
        "available": True,
        "method": kind,
        "grouping": grouping,
        "modelClass": type(model).__name__,
        "featureCount": len(features),
        "features": features,
        "note": "Global feature attribution over the whole training fit. Not per-prediction attribution — that needs SHAP over a background sample and is not computed here.",
    }))


if __name__ == "__main__":
    main()
