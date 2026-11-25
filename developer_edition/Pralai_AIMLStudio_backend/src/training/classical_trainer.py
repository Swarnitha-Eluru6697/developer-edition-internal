#!/usr/bin/env python3
"""
Real classical-ML training: dynamically imports the ACTUAL class the
catalog names (sklearn.ensemble.RandomForestClassifier,
xgboost.XGBClassifier, etc. — real, resolvable Python classes, not
placeholders) and genuinely fits it. This is the classical-ML
counterpart to real_trainer.py's deep-learning MLP — same honesty
standard: real scikit-learn-family training, real metrics, a real saved
model file.

Covers 60 of ML Studio's 78 non-LLM base models: all 51 sklearn:// entries,
plus 9 oci:// entries whose libraries (xgboost, lightgbm, catboost,
umap-learn) expose a real sklearn-compatible fit/predict or
fit_transform API. Explicitly does NOT cover 6 true time-series-only
libraries (Prophet, ARIMA, SARIMA, auto-ARIMA, exponential smoothing,
Holt-Winters) — those fit on a univariate series, not X/y tabular data,
a genuinely different shape this script doesn't attempt to fake.

Real dataset support: when `datasetPath` (a local CSV file, already
downloaded by the Node caller) is provided, this loads and fits on the
ACTUAL data — real column-aware preprocessing (numeric imputation +
scaling, categorical imputation + one-hot encoding, both real sklearn
transformers, not hand-rolled), a real train/test split of the real
rows, and the fitted preprocessor is saved alongside the model so
prediction can apply the exact same real transformation later. Falls
back to honestly-labeled synthetic data (unchanged from before) when no
dataset file is available — most base models still don't have one
uploaded, and training must still work for those.

Two honesty-affecting simplifications, disclosed here rather than hidden:
  - Synthetic classification data is taken as abs(X) so Naive Bayes
    variants (which require non-negative features) work through the same
    generic path as every other classifier. Real datasets are used as-is
    — a real dataset's actual sign is real information, not adjusted.
  - Synthetic regression targets are taken as abs(y) for the same reason
    (Poisson/Tweedie need non-negative targets). Real datasets: as-is.
  - "Ranking" and "Forecasting" (for the two library-supplied forecasters
    still in scope, xgboost/lightgbm) are both approximated as regression
    — real fitting, real metrics, but not true group-wise ranking or true
    time-series modeling. Labeled as an approximation in the result.
"""
import sys
import json
import time
import warnings
import os
import tempfile
import importlib
import contextlib

warnings.filterwarnings("ignore")


@contextlib.contextmanager
def _suppress_native_stdout():
    """Some libraries (LightGBM, CatBoost) write training logs straight to
    the OS-level stdout file descriptor from native code — a plain Python
    `contextlib.redirect_stdout` doesn't catch that, since it only
    reassigns `sys.stdout`, not the underlying fd the C/C++ layer writes
    to. This redirects fd 1 itself for the duration of the fit call, so
    the only thing this script ever actually prints is its own final
    JSON line.
    """
    stdout_fd = sys.stdout.fileno()
    saved_fd = os.dup(stdout_fd)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        sys.stdout.flush()
        os.dup2(devnull_fd, stdout_fd)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved_fd, stdout_fd)
        os.close(devnull_fd)
        os.close(saved_fd)


def import_class(classpath: str):
    module_path, class_name = classpath.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _n_rows(X):
    """Row count that works for a dense array AND a sparse matrix.

    `len()` on a scipy sparse array raises "sparse array length is ambiguous".
    Every one of these call sites was written when the feature matrix was always
    dense; keeping the matrix sparse for a wide one-hot made them all reachable.
    """
    return X.shape[0]


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


def load_real_data(dataset_path, target_column=None, dataset_format=None, features=None, preprocessing=None):
    """Loads a real dataset in whatever real format it actually is (CSV,
    Parquet, JSON, XLSX — see dataset_loader.py), fits a real
    ColumnTransformer (numeric -> impute+scale, categorical ->
    impute+one-hot) on it, and returns the transformed real X, real y
    (or None for unsupervised), the FITTED preprocessor (saved alongside
    the model so inference can reuse the identical real transformation),
    and the original column names.
    """
    import numpy as np
    from sklearn.compose import ColumnTransformer
    from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler, MinMaxScaler, LabelEncoder
    from pandas.api.types import is_numeric_dtype
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dataset_loader import load_tabular

    df = load_tabular(dataset_path, dataset_format)
    if target_column and target_column in df.columns:
        y_raw = df[target_column]
        X_df = df.drop(columns=[target_column])
    else:
        y_raw = None
        X_df = df

    # Honour the caller's feature allowlist. The Train wizard lets you switch
    # columns off (ids, leakage-prone fields) and stores that list on the run,
    # but training used to fit on every remaining column regardless — so the
    # toggles changed nothing, and the served model then demanded columns the
    # operator had explicitly excluded. A name that isn't in the file is an
    # error, not something to skip quietly: it means the run was configured
    # against a different schema than the data it is about to train on.
    if features:
        requested = [c for c in features if c != target_column]
        missing = [c for c in requested if c not in X_df.columns]
        if missing:
            raise ValueError(
                "requested feature column(s) not present in the dataset: %s. Available: %s"
                % (missing, list(X_df.columns))
            )
        if requested:
            X_df = X_df[requested]

    numeric_cols = X_df.select_dtypes(include=["number"]).columns.tolist()
    categorical_cols = X_df.select_dtypes(exclude=["number"]).columns.tolist()
    if not numeric_cols and not categorical_cols:
        raise ValueError("dataset has no usable columns after removing the target column")

    # ── Per-column preprocessing ─────────────────────────────────────────────
    # `preprocessing` is {column: strategy} chosen in the Train wizard. Anything
    # not named there falls back to the type-appropriate default, which is what
    # every run did before this was configurable — so an omitted column behaves
    # exactly as it always has.
    #
    # The fitted transformer is saved alongside the model, so serving applies
    # the identical transformation; changing a strategy here genuinely changes
    # what the deployed model expects, it is not a display preference.
    strategies = dict(preprocessing or {})
    unknown = [c for c in strategies if c not in X_df.columns]
    if unknown:
        raise ValueError(
            "preprocessing specified for column(s) not in the training set: %s. Available: %s"
            % (unknown, list(X_df.columns))
        )

    VALID = {
        "standard_scale", "min_max_scale", "one_hot", "ordinal", "passthrough", "none",
    }
    bad = {c: v for c, v in strategies.items() if v not in VALID}
    if bad:
        raise ValueError("unsupported preprocessing strategy: %s. Supported: %s" % (bad, sorted(VALID)))

    def strategy_for(col):
        if col in strategies:
            return strategies[col]
        return "standard_scale" if col in numeric_cols else "one_hot"

    def build(col):
        """One column -> (name, pipeline) using its chosen strategy. Imputation
        always runs first: a NaN would break every downstream estimator, and no
        strategy on offer means "keep the missing values"."""
        st = strategy_for(col)
        is_num = col in numeric_cols
        impute = SimpleImputer(strategy="median" if is_num else "most_frequent")
        if st == "standard_scale":
            return Pipeline([("impute", impute), ("scale", StandardScaler())])
        if st == "min_max_scale":
            return Pipeline([("impute", impute), ("scale", MinMaxScaler())])
        if st == "one_hot":
            return Pipeline([("impute", impute), ("onehot", OneHotEncoder(handle_unknown="ignore"))])
        if st == "ordinal":
            return Pipeline([
                ("impute", impute),
                ("ordinal", OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1)),
            ])
        # passthrough / none — still imputed, because a NaN reaching the
        # estimator is a crash, not a passthrough.
        return Pipeline([("impute", impute)])

    # Positional names: sklearn rejects "__" in a transformer name, and a column
    # name could contain one itself, so the index is the only safe key.
    transformers = [(f"c{i}", build(c), [c]) for i, c in enumerate(X_df.columns)]
    applied_strategies = {c: strategy_for(c) for c in X_df.columns}

    preprocessor = ColumnTransformer(transformers)
    X = _materialize_features(preprocessor.fit_transform(X_df), np)

    y = None
    label_encoder = None
    if y_raw is not None:
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
            label_encoder = LabelEncoder()
            y = label_encoder.fit_transform(y_raw.astype(str))
        else:
            y = y_raw.values
        # Defensively force a real plain numpy array — a pandas Series
        # backed by pyarrow (pandas' default string/object backend once
        # pyarrow is installed, which it now is for real Parquet support)
        # doesn't support sklearn's fancy indexing correctly in
        # train_test_split even after passing through LabelEncoder, in
        # this pandas/sklearn/pyarrow version combination. Confirmed as
        # a real regression, not a hypothetical one: installing pyarrow
        # for Parquet support silently broke this exact path before this
        # line existed — caught by re-running the existing real-dataset
        # test after adding Parquet support, not assumed safe.
        y = np.asarray(y)

    return X, y, preprocessor, label_encoder, list(X_df.columns), len(df), applied_strategies


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from runlog import log, describe_params
    try:
        raw = sys.stdin.read()
        req = json.loads(raw)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"invalid input: {e}"}))
        return

    classpath = req.get("classpath", "")
    task = req.get("task", "classification")
    seed = int(req.get("seed", 0)) or 42
    hp = req.get("hyperparams", {}) or {}
    dataset_path = req.get("datasetPath")
    dataset_format = req.get("datasetFormat")
    target_column = req.get("targetColumn")
    features = req.get("features") or None
    preprocessing = req.get("preprocessing") or None

    try:
        import numpy as np
        import joblib
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import (
            accuracy_score, f1_score, mean_squared_error, r2_score,
            silhouette_score, precision_score, recall_score, roc_auc_score,
        )
        from sklearn.datasets import make_classification, make_regression, make_blobs
        from sklearn.manifold import trustworthiness
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"scikit-learn/numpy/joblib not available: {e}"}))
        return

    try:
        Estimator = import_class(classpath)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"could not import '{classpath}': {e}"}))
        return

    np.random.seed(seed)
    start = time.time()
    log(f"Training {classpath} — task '{task}', seed {seed}", classpath=classpath, task=task, seed=seed,
        hyperparams=hp or "defaults", dataset=dataset_path and os.path.basename(dataset_path), target=target_column)
    real_preprocessor = None
    real_label_encoder = None
    real_data_note = None

    try:
        if task in ("classification", "classification / regression"):
            if dataset_path:
                X, y, real_preprocessor, real_label_encoder, feature_cols, n_rows, applied_preprocessing = load_real_data(dataset_path, target_column, dataset_format, features, preprocessing)
                real_data_note = f"REAL dataset ({n_rows} rows, target='{target_column}') — {len(feature_cols)} real feature column(s), real preprocessing fitted and saved alongside the model."
                vals, counts = np.unique(y, return_counts=True)
                log(f"Loaded {n_rows} rows · {len(feature_cols)} features · target '{target_column}'", features=list(feature_cols),
                    class_balance={str(v): int(c) for v, c in zip(vals, counts)}, preprocessing=applied_preprocessing)
            else:
                log("No dataset file — fitting on synthetic data", level="warn")
                X, y = make_classification(n_samples=600, n_features=14, n_informative=10, n_redundant=2, n_classes=2, random_state=seed)
                X = np.abs(X)  # see module docstring
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=seed)
            log(f"Split 75/25 — {_n_rows(X_train)} train / {_n_rows(X_test)} held-out test rows", train=_n_rows(X_train), test=_n_rows(X_test))
            model = _instantiate(Estimator, seed, hp)
            log(f"Fitting {type(model).__name__}", params=describe_params(model))
            fit_t0 = time.time()
            with _suppress_native_stdout():
                model.fit(X_train, y_train)
            log(f"Fit finished in {time.time() - fit_t0:.2f}s", level="ok")
            preds = model.predict(X_test)
            elapsed = time.time() - start
            result = {
                "ok": True, "measuredBy": f"{Estimator.__module__}.{Estimator.__name__} — real fit on {'a real dataset' if dataset_path else 'synthetic data'}",
                "note": real_data_note or "Synthetic data (no dataset file was uploaded for this training run); the FIT is real — real gradient/tree/analytic optimization, not a simulated number.",
                "taskRun": "classification", "approximation": task in ("ranking", "forecasting"), "realDataset": bool(dataset_path),
                "elapsedSeconds": round(elapsed, 3),
                "accuracy": round(float(accuracy_score(y_test, preds)) * 100, 2),
                "f1": round(float(f1_score(y_test, preds, average="weighted")) * 100, 2),
                # Precision and recall on the same held-out split as accuracy
                # and F1. Only accuracy and F1 were ever recorded before, so
                # the acceptance policy could never actually evaluate its
                # precision/recall thresholds against a real run — those rows
                # read "not recorded" for every model trained until now.
                # zero_division=0 rather than a crash on a class the model
                # never predicts: that is a real (bad) result, not an error.
                "precision": round(float(precision_score(y_test, preds, average="weighted", zero_division=0)) * 100, 2),
                "recall": round(float(recall_score(y_test, preds, average="weighted", zero_division=0)) * 100, 2),
                # The train-split score exists only to make the generalization
                # gap measurable. It is a fit statistic, not a quality claim,
                # and the gate treats it as such.
                "trainAccuracy": round(float(accuracy_score(y_train, model.predict(X_train))) * 100, 2),
                "trainSamples": _n_rows(X_train), "testSamples": _n_rows(X_test),
            }
            # ROC-AUC needs scores, not labels, and is only defined when the
            # test split actually contains more than one class. Estimators
            # without predict_proba/decision_function (and multiclass cases
            # where the shapes don't line up) simply don't report it — absent
            # is honest, an invented 0.5 would not be.
            try:
                classes = np.unique(y_test)
                if len(classes) > 1:
                    if hasattr(model, "predict_proba"):
                        proba = model.predict_proba(X_test)
                        auc = roc_auc_score(y_test, proba[:, 1]) if len(classes) == 2 else roc_auc_score(y_test, proba, multi_class="ovr", average="weighted")
                    elif hasattr(model, "decision_function") and len(classes) == 2:
                        auc = roc_auc_score(y_test, model.decision_function(X_test))
                    else:
                        auc = None
                    if auc is not None:
                        result["rocAuc"] = round(float(auc), 4)
            except Exception:
                pass

        elif task in ("regression", "ranking", "forecasting"):
            if dataset_path:
                X, y, real_preprocessor, real_label_encoder, feature_cols, n_rows, applied_preprocessing = load_real_data(dataset_path, target_column, dataset_format, features, preprocessing)
                real_data_note = f"REAL dataset ({n_rows} rows, target='{target_column}') — {len(feature_cols)} real feature column(s), real preprocessing fitted and saved alongside the model."
                log(f"Loaded {n_rows} rows · {len(feature_cols)} features · target '{target_column}'", features=list(feature_cols),
                    target_stats={"min": float(np.min(y)), "mean": float(np.mean(y)), "max": float(np.max(y))}, preprocessing=applied_preprocessing)
            else:
                log("No dataset file — fitting on synthetic data", level="warn")
                X, y = make_regression(n_samples=600, n_features=12, noise=12, random_state=seed)
                y = np.abs(y)  # see module docstring
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=seed)
            log(f"Split 75/25 — {_n_rows(X_train)} train / {_n_rows(X_test)} held-out test rows", train=_n_rows(X_train), test=_n_rows(X_test))
            model = _instantiate(Estimator, seed, hp)
            log(f"Fitting {type(model).__name__}", params=describe_params(model))
            fit_t0 = time.time()
            with _suppress_native_stdout():
                model.fit(X_train, y_train)
            log(f"Fit finished in {time.time() - fit_t0:.2f}s", level="ok")
            preds = model.predict(X_test)
            elapsed = time.time() - start
            result = {
                "ok": True, "measuredBy": f"{Estimator.__module__}.{Estimator.__name__} — real fit on {'a real dataset' if dataset_path else 'synthetic data'}",
                "note": real_data_note or "Synthetic data (no dataset file was uploaded for this training run); the FIT is real.",
                "taskRun": "regression", "approximation": task in ("ranking", "forecasting"), "realDataset": bool(dataset_path),
                "elapsedSeconds": round(elapsed, 3),
                "mse": round(float(mean_squared_error(y_test, preds)), 3),
                "r2": round(float(r2_score(y_test, preds)), 4),
                # Same purpose as trainAccuracy above: the generalization gap
                # the acceptance policy checks needs both sides of the split.
                "trainR2": round(float(r2_score(y_train, model.predict(X_train))), 4),
                "trainSamples": _n_rows(X_train), "testSamples": _n_rows(X_test),
            }

        elif task == "clustering":
            if dataset_path:
                X, _, real_preprocessor, _, feature_cols, n_rows, applied_preprocessing = load_real_data(dataset_path, None, dataset_format, features, preprocessing)
                real_data_note = f"REAL dataset ({n_rows} rows) — {len(feature_cols)} real feature column(s), real preprocessing fitted and saved alongside the model."
            else:
                X, _ = make_blobs(n_samples=600, centers=4, n_features=10, random_state=seed)
            model = _instantiate(Estimator, seed, hp)
            with _suppress_native_stdout():
                labels = model.fit_predict(X) if hasattr(model, "fit_predict") else model.fit(X).predict(X)
            elapsed = time.time() - start
            n_clusters_found = len(set(labels)) - (1 if -1 in labels else 0)
            sil = float(silhouette_score(X, labels)) if n_clusters_found > 1 else None
            result = {
                "ok": True, "measuredBy": f"{Estimator.__module__}.{Estimator.__name__} — real fit on {'a real dataset' if dataset_path else 'synthetic data'}",
                "note": real_data_note or "Synthetic data; the clustering FIT and resulting assignment are real.",
                "taskRun": "clustering", "approximation": False, "realDataset": bool(dataset_path),
                "elapsedSeconds": round(elapsed, 3),
                "clustersFound": int(n_clusters_found),
                "silhouetteScore": round(sil, 4) if sil is not None else None,
                "samples": _n_rows(X),
            }

        elif task == "anomaly_detection":
            if dataset_path:
                # Real data has no known ground truth to inject and check
                # against — the synthetic path's "detection rate vs
                # actually-injected outliers" claim genuinely doesn't
                # apply here. Report the real, honest thing instead: what
                # fraction of the real data the model actually flagged.
                X, _, real_preprocessor, _, feature_cols, n_rows, applied_preprocessing = load_real_data(dataset_path, None, dataset_format, features, preprocessing)
                real_data_note = f"REAL dataset ({n_rows} rows) — no ground-truth anomaly labels exist for real data, so this reports the real flagged fraction, not a detection rate against known outliers (see the synthetic-data path for that)."
                model = _instantiate(Estimator, seed, hp)
                # LOF in its default mode can only label the rows it was fitted on;
                # novelty mode is what lets the saved model score new rows when served.
                if type(model).__name__ == "LocalOutlierFactor":
                    model.set_params(novelty=True)
                with _suppress_native_stdout():
                    if hasattr(model, "fit_predict"):
                        raw_preds = model.fit_predict(X)
                    else:
                        model.fit(X)
                        raw_preds = model.predict(X)
                y_pred = (raw_preds == -1).astype(int)
                elapsed = time.time() - start
                result = {
                    "ok": True, "measuredBy": f"{Estimator.__module__}.{Estimator.__name__} — real fit on a real dataset",
                    "note": real_data_note, "taskRun": "anomaly_detection", "approximation": False, "realDataset": True,
                    "elapsedSeconds": round(elapsed, 3),
                    "flaggedCount": int(y_pred.sum()),
                    "flaggedRate": round(float(y_pred.mean()) * 100, 2),
                    "samples": _n_rows(X),
                }
            else:
                X_normal, _ = make_blobs(n_samples=550, centers=3, n_features=10, random_state=seed, cluster_std=1.0)
                rng = np.random.RandomState(seed)
                X_outliers = rng.uniform(low=X_normal.min() * 3, high=X_normal.max() * 3, size=(50, X_normal.shape[1]))
                X = np.vstack([X_normal, X_outliers])
                y_true = np.array([0] * len(X_normal) + [1] * len(X_outliers))
                model = _instantiate(Estimator, seed, hp)
                with _suppress_native_stdout():
                    if hasattr(model, "fit_predict"):
                        raw_preds = model.fit_predict(X)
                    else:
                        model.fit(X)
                        raw_preds = model.predict(X)
                y_pred = (raw_preds == -1).astype(int)
                elapsed = time.time() - start
                tp = int(((y_pred == 1) & (y_true == 1)).sum())
                detected_frac = tp / max(int(y_true.sum()), 1)
                result = {
                    "ok": True, "measuredBy": f"{Estimator.__module__}.{Estimator.__name__} — real fit on synthetic data",
                    "note": "50 real injected outliers among 550 normal points; the FIT and detection are real, checked against the actual injected ground truth.",
                    "taskRun": "anomaly_detection", "approximation": False, "realDataset": False,
                    "elapsedSeconds": round(elapsed, 3),
                    "injectedOutliers": int(y_true.sum()),
                    "trueOutliersDetected": tp,
                    "detectionRate": round(detected_frac * 100, 1),
                    "samples": _n_rows(X),
                }

        elif task == "dimensionality_reduction":
            if dataset_path:
                X, _, real_preprocessor, _, feature_cols, n_rows, applied_preprocessing = load_real_data(dataset_path, None, dataset_format, features, preprocessing)
                real_data_note = f"REAL dataset ({n_rows} rows) — {len(feature_cols)} real feature column(s), real preprocessing fitted and saved alongside the model."
            else:
                X, _ = make_classification(n_samples=500, n_features=20, n_informative=14, random_state=seed)
            dr_hp = dict(hp)
            dr_hp.setdefault("n_components", 2 if Estimator.__name__ == "TSNE" else min(5, X.shape[1] - 1) if X.shape[1] > 1 else 1)
            model = _instantiate(Estimator, seed, dr_hp)
            with _suppress_native_stdout():
                X_reduced = model.fit_transform(X)
            elapsed = time.time() - start
            explained = None
            if hasattr(model, "explained_variance_ratio_"):
                explained = round(float(sum(model.explained_variance_ratio_)) * 100, 2)
            X_reduced_2d = X_reduced if X_reduced.ndim > 1 else X_reduced.reshape(-1, 1)
            n_neighbors = min(5, _n_rows(X) - 1)
            trust = float(trustworthiness(X, X_reduced_2d, n_neighbors=n_neighbors))
            result = {
                "ok": True, "measuredBy": f"{Estimator.__module__}.{Estimator.__name__} — real fit on {'a real dataset' if dataset_path else 'synthetic data'}",
                "note": real_data_note or "Synthetic data; the reduction is real.",
                "taskRun": "dimensionality_reduction", "approximation": False, "realDataset": bool(dataset_path),
                "elapsedSeconds": round(elapsed, 3),
                "inputDimensions": X.shape[1],
                "outputDimensions": X_reduced_2d.shape[1],
                "explainedVariancePct": explained,
                "trustworthiness": round(trust, 4),
                "samples": _n_rows(X),
            }

        else:
            print(json.dumps({"ok": False, "error": f"unrecognized task '{task}'"}))
            return

    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__} during real fit of {classpath}: {e}"}))
        return

    model_dir = tempfile.mkdtemp(prefix="ml-studio-classical-")
    model_path = os.path.join(model_dir, "model.joblib")
    # Real preprocessor (and label encoder, for classification on
    # string-labeled real data) saved alongside the model — previously
    # only the raw model was ever saved, so real-dataset inference could
    # never apply the same real transformation training actually used.
    joblib.dump({"model": model, "task": task, "classpath": classpath, "preprocessor": real_preprocessor, "labelEncoder": real_label_encoder}, model_path)
    result["modelFilePath"] = model_path
    result["modelFileSizeBytes"] = os.path.getsize(model_path)
    result["classpath"] = classpath
    # Every hyperparameter the fitted estimator actually has — overrides and library defaults alike.
    try:
        # NaN (e.g. XGBoost's missing=nan) isn't valid JSON and would break parsing of this result.
        result["effectiveHyperparams"] = {k: ("nan" if isinstance(v, float) and v != v else v if isinstance(v, (int, float, str, bool)) or v is None else str(v))
                                          for k, v in model.get_params(deep=False).items()}
    except Exception:
        pass
    # The split this run ACTUALLY used (the Train wizard's requested split is not applied here):
    # supervised tasks hold out a seeded random 25%; unsupervised ones fit and score every row.
    supervised = task in ("classification", "regression", "ranking", "forecasting")  # all fit on a 75 / 25 split
    if supervised:
        result["split"] = {"train": _n_rows(X_train), "validation": 0, "test": _n_rows(X_test), "unit": "rows",
                           "method": "random 75 / 25 (seeded)", "evaluatedOn": "test"}
    else:
        result["split"] = {"train": _n_rows(X), "validation": 0, "test": 0, "unit": "rows",
                           "method": "none — fitted and scored on every row", "evaluatedOn": "training rows"}

    if result.get("ok"):
        keys = ("accuracy", "f1", "precision", "recall", "rocAuc", "trainAccuracy", "mse", "rmse", "mae", "r2", "silhouette")
        where = "held-out test rows" if supervised else "all fitted rows (nothing held out)"
        log(f"Metrics on {where}: " + ", ".join(f"{k} {result[k]}" for k in keys if k in result), level="ok",
            metrics={k: result[k] for k in keys if k in result})
        try:
            imp = getattr(model, "feature_importances_", None)
            if imp is not None and dataset_path and len(imp) == len(feature_cols):
                top = sorted(zip(feature_cols, imp), key=lambda x: -x[1])[:8]
                log("Top features: " + ", ".join(f"{f} {w:.3f}" for f, w in top), top_features={f: round(float(w), 4) for f, w in top})
        except Exception:
            pass
    else:
        log(f"Training failed: {result.get('error')}", level="error")
    print(json.dumps(result))


def _instantiate(Estimator, seed, hp):
    """Try real hyperparameters first; fall back to bare defaults if the
    class doesn't accept them (e.g. no random_state param) — never
    silently substitutes a different class, only adjusts constructor
    kwargs. Falls back one parameter at a time (hp+seed -> hp alone ->
    bare) rather than jumping straight to bare defaults the moment ANY
    single kwarg is rejected.
    """
    for kwargs in (
        {**hp, "random_state": seed},
        dict(hp),
        {"random_state": seed},
        {},
    ):
        try:
            return Estimator(**kwargs)
        except TypeError:
            continue
    return Estimator()


if __name__ == "__main__":
    main()
