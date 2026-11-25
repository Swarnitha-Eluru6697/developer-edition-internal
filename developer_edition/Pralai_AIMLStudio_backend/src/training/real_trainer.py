#!/usr/bin/env python3
"""
Real, measured training telemetry — not derived, not estimated.

This trains an actual scikit-learn MLPClassifier/MLPRegressor and reports
its genuinely measured loss_curve_, validation_scores_, n_iter_, and
final weights. It is NOT a PyTorch/TensorFlow GPU training run — this
environment has no GPU and no connected training cluster, so claiming one
would be a fabrication. scikit-learn is used here as a real, honest
stand-in trainer: every number in the output was actually computed by a
real training loop, on real (synthetically generated, since no actual
dataset file is wired into this scaffold) data — the loss curve is real
measured optimizer behavior, not a curve shaped to match a target endpoint.

Usage: python3 real_trainer.py < input.json > output.json
Input:  {"task": "classification"|"regression", "hyperparams": {...}, "seed": int}
Output: {"ok": true, "measuredBy": "scikit-learn MLPClassifier", ...} or {"ok": false, "error": "..."}
"""
import sys
import json
import time
import warnings
import os
import tempfile

warnings.filterwarnings("ignore")


def _materialize_features(X, np, dtype=float, dense_budget_bytes=2 * 1024**3):
    """Turn the ColumnTransformer's output into something an estimator can fit.

    OneHotEncoder returns a SPARSE matrix, and this used to call `.toarray()` on
    it unconditionally. That is fine for a handful of categories and catastrophic
    for a real one: a 1,000,163-row dataset with an 8,011-value city column
    expands to 8.0 billion cells — 60 GB dense, against 0.04 GB sparse. The
    process was simply SIGKILLed by the OS, and the run reported
    "classical_trainer.py exited null" with nothing else to go on.

    So: densify only when the dense form genuinely fits the budget, and otherwise
    hand the estimator the sparse matrix it was already given. scikit-learn's
    linear models and tree ensembles, and XGBoost/LightGBM, all accept CSR
    directly. An estimator that does not will raise on fit, and the caller turns
    that into a message naming the real cause rather than an OOM kill.
    """
    if hasattr(X, "toarray"):
        rows, cols = X.shape
        if rows * cols * np.dtype(dtype).itemsize <= dense_budget_bytes:
            return np.asarray(X.toarray(), dtype=dtype)
        return X.tocsr().astype(dtype)
    return np.asarray(X, dtype=dtype)


def main():
    try:
        raw = sys.stdin.read()
        req = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"invalid input JSON: {e}"}))
        return

    task = req.get("task", "classification")
    seed = int(req.get("seed", 0)) or 42
    hp = req.get("hyperparams", {}) or {}
    hidden = tuple(hp.get("hiddenLayers", [64, 32]))
    max_iter = int(hp.get("maxIter", 200))
    dataset_path = req.get("datasetPath")
    target_column = req.get("targetColumn")

    try:
        import numpy as np
        import joblib
        from sklearn.neural_network import MLPClassifier, MLPRegressor
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score, f1_score, mean_squared_error, r2_score
        from sklearn.preprocessing import StandardScaler
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"scikit-learn/numpy/joblib not available: {e}"}))
        return

    rng = np.random.RandomState(seed)
    real_preprocessor = None
    real_label_encoder = None
    real_data_note = None

    if dataset_path:
        # Real dataset support — same real ColumnTransformer approach as
        # classical_trainer.py, for the same reason: real column-aware
        # preprocessing (numeric impute+scale, categorical impute+
        # one-hot), fitted on the real data and saved alongside the
        # model so inference can apply the identical real transformation.
        try:
            from sklearn.compose import ColumnTransformer
            from sklearn.preprocessing import OneHotEncoder, LabelEncoder
            from pandas.api.types import is_numeric_dtype
            from sklearn.impute import SimpleImputer
            from sklearn.pipeline import Pipeline
            import sys as _sys, os as _os
            _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
            from dataset_loader import load_tabular

            df = load_tabular(dataset_path, req.get("datasetFormat"))
            if target_column and target_column in df.columns:
                y_raw = df[target_column]
                X_df = df.drop(columns=[target_column])
            else:
                print(json.dumps({"ok": False, "error": f"real dataset provided but target column '{target_column}' isn't in it — real deep-learning training needs a real target, unlike unsupervised classical tasks"}))
                return

            numeric_cols = X_df.select_dtypes(include=["number"]).columns.tolist()
            categorical_cols = X_df.select_dtypes(exclude=["number"]).columns.tolist()
            transformers = []
            if numeric_cols:
                transformers.append(("num", Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]), numeric_cols))
            if categorical_cols:
                transformers.append(("cat", Pipeline([("impute", SimpleImputer(strategy="most_frequent")), ("onehot", OneHotEncoder(handle_unknown="ignore"))]), categorical_cols))
            real_preprocessor = ColumnTransformer(transformers)
            X = _materialize_features(real_preprocessor.fit_transform(X_df), np)

            # Encode unless the target is genuinely numeric.
            #
            # This used to test `dtype == object`. pandas 3.0 made `str` the default
            # dtype for string columns, so that test became False for EVERY string
            # column — the LabelEncoder was skipped and raw strings went straight to
            # the estimator, which then reported a confusing type error naming its
            # own expected class indices. It broke every string-labelled
            # classification target, not just pathological ones.
            #
            # `is_numeric_dtype` is the property that actually matters and does not
            # depend on which string backend pandas picked (object / str /
            # string[pyarrow] / category all encode; int / float / bool do not).
            if not is_numeric_dtype(y_raw):
                real_label_encoder = LabelEncoder()
                y = real_label_encoder.fit_transform(y_raw.astype(str))
            else:
                y = y_raw.values
            y = np.asarray(y)  # see classical_trainer.py's load_real_data — same real pyarrow-backed-array fix
            real_data_note = f"REAL dataset ({len(df)} rows, target='{target_column}') — {len(X_df.columns)} real feature column(s), real preprocessing fitted and saved alongside the model."
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__} loading real dataset '{dataset_path}': {e}"}))
            return
    else:
        # Synthetic data — no real dataset file was uploaded for this
        # training run. What's real regardless: the actual gradient-
        # descent training loop, the actual per-iteration loss it
        # measured, the actual final weights.
        n_samples, n_features = 2000, 12
        X = rng.randn(n_samples, n_features)
        true_weights = rng.randn(n_features)
        if task == "regression":
            y = X @ true_weights + rng.randn(n_samples) * 0.5
        else:
            logits = X @ true_weights
            y = (logits + rng.randn(n_samples) * 1.5 > np.median(logits)).astype(int)

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=seed)
    scaler = None
    if real_preprocessor is None:
        # Synthetic path only — real_preprocessor already scaled real
        # data above; scaling it again here would double-transform it.
        scaler = StandardScaler()
        X_train = scaler.fit_transform(X_train)
        X_test = scaler.transform(X_test)

    start = time.time()
    common = dict(
        hidden_layer_sizes=hidden, max_iter=max_iter, random_state=seed,
        early_stopping=True, validation_fraction=0.15, n_iter_no_change=8,
        solver="adam", learning_rate_init=float(hp.get("learningRate", 0.001)),
    )
    if task == "regression":
        model = MLPRegressor(**common)
    else:
        model = MLPClassifier(**common)

    model.fit(X_train, y_train)
    elapsed = time.time() - start

    preds = model.predict(X_test)
    if task == "regression":
        final_metric = {"mse": round(float(mean_squared_error(y_test, preds)), 4), "r2": round(float(r2_score(y_test, preds)), 4)}
        train_preds = model.predict(X_train)
        train_metric_val = round(float(r2_score(y_train, train_preds)), 4)
        val_metric_val = final_metric["r2"]
    else:
        acc = accuracy_score(y_test, preds)
        f1 = f1_score(y_test, preds, average="weighted")
        final_metric = {"accuracy": round(float(acc) * 100, 2), "f1": round(float(f1) * 100, 2)}
        train_preds = model.predict(X_train)
        train_metric_val = round(float(accuracy_score(y_train, train_preds)) * 100, 2)
        val_metric_val = final_metric["accuracy"]

    # REAL weight statistics from the actual trained network.
    total_params = sum(w.size for w in model.coefs_) + sum(b.size for b in model.intercepts_)
    all_weights = np.concatenate([w.flatten() for w in model.coefs_])
    weight_norm = float(np.linalg.norm(all_weights))
    weight_mean = float(np.mean(all_weights))
    weight_std = float(np.std(all_weights))

    # loss_curve_ is scikit-learn's REAL per-iteration training loss — not
    # synthesized. validation_scores_ (with early_stopping=True) is the
    # REAL per-iteration validation score it actually computed.
    loss_curve = [round(float(v), 5) for v in model.loss_curve_]
    val_curve_raw = getattr(model, "validation_scores_", None)
    val_curve = [round(float(v), 5) for v in val_curve_raw] if val_curve_raw is not None else None

    # Actually save the real trained model — this is the piece that was
    # previously missing entirely: real gradient descent happened, real
    # weights existed in memory, and then were discarded when this
    # process exited. joblib.dump() persists the real fitted estimator
    # (weights, biases, learned scaler) to a real local file; the caller
    # (registerTrainedModel, via realTrainer.ts) uploads that file to the
    # customer's configured object storage. scaler is saved alongside the
    # model since inference needs the same real fitted transform the
    # training data went through, not just the raw model weights.
    model_dir = tempfile.mkdtemp(prefix="ml-studio-model-")
    model_path = os.path.join(model_dir, "model.joblib")
    joblib.dump({"model": model, "scaler": scaler, "task": task, "preprocessor": real_preprocessor, "labelEncoder": real_label_encoder}, model_path)
    model_size_bytes = os.path.getsize(model_path)

    result = {
        "ok": True,
        "measuredBy": f"scikit-learn {model.__class__.__name__} — real training loop, real loss curve on {'a real dataset' if dataset_path else 'synthetic data'}, not derived from a final number",
        "note": real_data_note or "This trains a real network on synthetically generated data (no dataset file was uploaded for this training run) — the DATA is synthetic, but the TRAINING PROCESS and every curve/statistic below is genuinely measured from real gradient descent, on CPU (no GPU is used or available in this environment).",
        "realDataset": bool(dataset_path),
        "architecture": f"MLP {' -> '.join(str(h) for h in hidden)} (scikit-learn, CPU)",
        "realEpochsRun": int(model.n_iter_),
        "elapsedSeconds": round(elapsed, 3),
        "lossType": "Log loss (cross-entropy)" if task != "regression" else "Squared error",
        "trainLossCurve": loss_curve,
        "validationScoreCurve": val_curve,
        "finalTrainLoss": loss_curve[-1] if loss_curve else None,
        "bestValidationScore": float(getattr(model, "best_validation_score_", val_curve[-1] if val_curve else 0)),
        "finalMetrics": final_metric,
        "trainMetricValue": train_metric_val,
        "validationMetricValue": val_metric_val,
        "totalParameters": int(total_params),
        # shape[0], not len(): this matrix stays sparse for a wide one-hot, and
        # len() on a scipy sparse array raises "length is ambiguous".
        "trainSamples": X_train.shape[0], "testSamples": X_test.shape[0],
        "weightNorm": round(weight_norm, 3),
        "weightMean": round(weight_mean, 5),
        "weightStdDev": round(weight_std, 5),
        "solver": "adam",
        "learningRateInit": common["learning_rate_init"],
        "earlyStoppingTriggered": bool(model.n_iter_ < max_iter),
        "modelFilePath": model_path,
        "modelFileSizeBytes": model_size_bytes,
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
