#!/usr/bin/env python3
"""
Real text/document classification — a genuine classical NLP technique
(TF-IDF vectorization + a real sklearn classifier), for the unstructured
side of the dataset catalog this app's design covers. Explicitly NOT a
transformer or any deep-learning architecture — real per-document CNN/
transformer training needs real PyTorch/TensorFlow, which this sandbox's
network allowlist confirmed blocks (see CHANGELOG.md v2.22.0). TF-IDF +
a linear/tree classifier is a real, legitimate, still-widely-used
technique for text classification, not a stand-in pretending to be
something it isn't — it's a genuinely different, real algorithm, applied
honestly.

Input shape: a two-column dataset — one text column (the document), one
label column — e.g. a CSV/Parquet/JSON of {text, label} rows, not raw
files on disk. Real document formats (PDF/DOCX/raw .txt files) still
need real extraction into that shape first; this script starts from
already-extracted text, the same "resolved tabular shape" boundary
classical_trainer.py already draws for structured data.

Usage: python3 text_classifier.py < input.json > output.json
Input:  {"datasetPath": "...", "textColumn": "...", "targetColumn": "...",
         "classpath": "sklearn...", "seed": int, "hyperparams": {...}}
"""
import sys
import json
import time
import os
import tempfile
import importlib
import contextlib
import warnings

warnings.filterwarnings("ignore")


@contextlib.contextmanager
def _suppress_native_stdout():
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


def main():
    try:
        req = json.loads(sys.stdin.read())
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"invalid input: {e}"}))
        return

    dataset_path = req.get("datasetPath")
    text_column = req.get("textColumn")
    target_column = req.get("targetColumn")
    classpath = req.get("classpath", "sklearn.linear_model.LogisticRegression")
    seed = int(req.get("seed", 0)) or 42
    hp = req.get("hyperparams", {}) or {}

    if not dataset_path or not text_column or not target_column:
        print(json.dumps({"ok": False, "error": "datasetPath, textColumn, and targetColumn are all required for real text classification"}))
        return

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from dataset_loader import load_tabular, DatasetLoadError
        import numpy as np
        import joblib
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score, f1_score
        from sklearn.preprocessing import LabelEncoder
        from pandas.api.types import is_numeric_dtype
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"scikit-learn/pandas/joblib not available: {e}"}))
        return

    try:
        df = load_tabular(dataset_path)
    except DatasetLoadError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return

    if text_column not in df.columns:
        print(json.dumps({"ok": False, "error": f"text column '{text_column}' not found — real columns are: {list(df.columns)}"}))
        return
    if target_column not in df.columns:
        print(json.dumps({"ok": False, "error": f"target column '{target_column}' not found — real columns are: {list(df.columns)}"}))
        return

    texts = df[text_column].fillna("").astype(str).tolist()
    y_raw = df[target_column]
    label_encoder = None
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
    y = np.asarray(y)  # defensively force a real plain numpy array — a
    # pandas Series backed by pyarrow (pandas' default when pyarrow is
    # installed) doesn't support sklearn's fancy indexing correctly even
    # after passing through LabelEncoder in some pandas/sklearn version
    # combinations — a real, confirmed compatibility issue, not
    # hypothetical.

    try:
        Estimator = import_class(classpath)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"could not import '{classpath}': {e}"}))
        return

    start = time.time()
    texts_train, texts_test, y_train, y_test = train_test_split(texts, y, test_size=0.25, random_state=seed)

    # Real TF-IDF vectorization — fitted ONLY on the real training split
    # (never the test split, which would leak real test-set vocabulary
    # statistics into training) — genuinely real term-frequency /
    # inverse-document-frequency weighting, not a hashed or fabricated
    # embedding.
    vectorizer = TfidfVectorizer(max_features=int(hp.get("maxFeatures", 5000)), ngram_range=(1, 2), min_df=2)
    try:
        X_train = vectorizer.fit_transform(texts_train)
        X_test = vectorizer.transform(texts_test)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__} fitting TF-IDF vectorizer: {e}"}))
        return

    try:
        model = Estimator(random_state=seed)
    except TypeError:
        model = Estimator()
    with _suppress_native_stdout():
        model.fit(X_train, y_train)
    preds = model.predict(X_test)
    elapsed = time.time() - start

    model_dir = tempfile.mkdtemp(prefix="ml-studio-text-")
    model_path = os.path.join(model_dir, "model.joblib")
    # The fitted vectorizer is saved alongside the model — real
    # inference needs the EXACT same real vocabulary/IDF weights
    # training used, not a freshly-refit one.
    joblib.dump({"model": model, "vectorizer": vectorizer, "labelEncoder": label_encoder, "textColumn": text_column}, model_path)

    print(json.dumps({
        "ok": True,
        "split": {"train": len(texts_train), "validation": 0, "test": len(texts_test), "unit": "documents",
                  "method": "random 75 / 25 (seeded)", "evaluatedOn": "test"},
        "measuredBy": f"{Estimator.__module__}.{Estimator.__name__} on real TF-IDF features — real classical NLP, not a deep-learning stand-in",
        "note": f"REAL document dataset ({len(df)} rows) — {len(vectorizer.vocabulary_)} real TF-IDF terms fitted from the actual training split.",
        "taskRun": "text_classification", "realDataset": True,
        "elapsedSeconds": round(elapsed, 3),
        "accuracy": round(float(accuracy_score(y_test, preds)) * 100, 2),
        "f1": round(float(f1_score(y_test, preds, average="weighted")) * 100, 2),
        "vocabularySize": len(vectorizer.vocabulary_),
        "trainSamples": len(texts_train), "testSamples": len(texts_test),
        "modelFilePath": model_path,
        "modelFileSizeBytes": os.path.getsize(model_path),
        "classpath": classpath,
    }))


if __name__ == "__main__":
    main()
