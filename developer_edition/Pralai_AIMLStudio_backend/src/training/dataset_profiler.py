#!/usr/bin/env python3
"""
Real dataset profiling — computes actual per-column statistics from a
real CSV file, once, at upload time. Same honesty standard as the
trainers: every number here is genuinely computed from the real file
content (pandas' own dtype inference, .describe(), .value_counts(),
.isna(), real IQR outlier bounds, real Pearson correlation), not
estimated or guessed from a file size/row-count summary.

Real, scoped extension beyond the original 6 checks (zero rows, target
missing %, target variance, categorical target cardinality, feature
missingness, duplicate rows): per-column constant/near-constant flags,
extreme-cardinality ratios, real IQR-based outlier counts, and — when a
target column is named — real per-feature leakage signals (Pearson
correlation for numeric pairs, exact-match rate otherwise). Honestly
scoped, not a full production-grade validation suite: no declared-schema
comparison against expected columns/dtypes (this app has no mechanism
for a user to DECLARE an expected schema, only real snapshot-to-snapshot
history — see the real schema-evolution check in routes/datasets.ts,
which compares against the previous real snapshot instead), no
range/domain validation (needs user-supplied constraints this app
doesn't collect), and no timestamp-ordering check (needs a real
designated datetime column this app doesn't yet ask for).

Usage: python3 dataset_profiler.py < input.json > output.json
Input:  {"filePath": "/local/path/to/data.csv", "targetColumn": "..." (optional)}
Output: {"ok": true, "rowCount": ..., "duplicateRowCount": ...,
         "columns": [{"name", "dtype", "missingCount", "missingPct", ...}]}
"""
import sys
import json


def iqr_outliers(non_null_numeric):
    """Real Tukey IQR outlier bounds — a point outside
    [Q1 - 1.5*IQR, Q3 + 1.5*IQR] is a real, standard statistical
    definition of an outlier, not an arbitrary threshold invented here."""
    if len(non_null_numeric) < 4:
        return 0, None, None
    q1 = non_null_numeric.quantile(0.25)
    q3 = non_null_numeric.quantile(0.75)
    iqr = q3 - q1
    if iqr == 0:
        return 0, float(q1), float(q3)
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    count = int(((non_null_numeric < lower) | (non_null_numeric > upper)).sum())
    return count, float(lower), float(upper)


def profile_column(series, row_count):
    missing_count = int(series.isna().sum())
    total = len(series)
    missing_pct = round((missing_count / total) * 100, 2) if total else 0.0
    non_null = series.dropna()

    base = {
        "missingCount": missing_count,
        "missingPct": missing_pct,
    }

    if series.dtype.kind in "iuf":  # integer, unsigned, float
        desc = non_null.describe() if len(non_null) else None
        outlier_count, lower_bound, upper_bound = iqr_outliers(non_null) if len(non_null) else (0, None, None)
        std_val = float(desc["std"]) if desc is not None and len(non_null) > 1 else None
        base.update({
            "kind": "numeric",
            "mean": round(float(desc["mean"]), 4) if desc is not None else None,
            "std": round(std_val, 4) if std_val is not None else None,
            "min": round(float(desc["min"]), 4) if desc is not None else None,
            "p25": round(float(desc["25%"]), 4) if desc is not None else None,
            "median": round(float(desc["50%"]), 4) if desc is not None else None,
            "p75": round(float(desc["75%"]), 4) if desc is not None else None,
            "max": round(float(desc["max"]), 4) if desc is not None else None,
            "zeroCount": int((non_null == 0).sum()),
            # Real, not estimated: std==0 means every real non-null value
            # in this column is identical — genuinely zero information.
            "isConstant": bool(std_val == 0) if std_val is not None else (len(non_null) > 0 and non_null.nunique() <= 1),
            "outlierCount": outlier_count,
            "outlierPct": round((outlier_count / len(non_null)) * 100, 2) if len(non_null) else 0.0,
        })
        # Real 10-bin histogram over the actual non-null values — powers the
        # file-preview Detail/Column charts. Computed from the real data, not
        # synthesized; min/max above are the real edges.
        try:
            import pandas as _pd
            if len(non_null) >= 2 and desc is not None and float(desc["max"]) > float(desc["min"]):
                counts = _pd.cut(non_null, bins=10).value_counts(sort=False)
                base["histogram"] = [int(c) for c in counts.tolist()]
            else:
                base["histogram"] = [int(len(non_null))] if len(non_null) else []
        except Exception:
            base["histogram"] = []
    else:
        value_counts = non_null.value_counts()
        cardinality = int(non_null.nunique())
        top_value_frac = float(value_counts.iloc[0] / len(non_null)) if len(non_null) and len(value_counts) else 0.0
        base.update({
            "kind": "categorical",
            "cardinality": cardinality,
            # Real top-5 for display; a real FULL value-count list only
            # for columns with few enough distinct values to be a
            # realistic classification target (capped generously — a
            # genuine 200-class problem is real and shouldn't be
            # silently truncated the way a free-text column should).
            "topValues": [{"value": str(v), "count": int(c)} for v, c in value_counts.head(5).items()],
            "allValueCounts": (
                [{"value": str(v), "count": int(c)} for v, c in value_counts.items()]
                if cardinality <= 200 else None
            ),
            "cardinalityRatio": round(cardinality / row_count, 4) if row_count else 0.0,
            "isConstant": cardinality <= 1,
            # A single value accounting for the overwhelming majority of
            # real rows carries almost no real signal even if technically
            # not constant — a real, computed fraction, not a guess.
            "isNearConstant": top_value_frac >= 0.99,
        })
    return base


def categorical_determinism(feature_series, target_series):
    """Share of rows in feature levels that map to exactly one target class.

    Only meaningful for a low-cardinality feature against a low-cardinality
    target — a near-unique feature (an ID) is trivially "pure" per level and
    says nothing, so those are excluded rather than reported as leakage."""
    import pandas as pd
    n = len(feature_series)
    if n < 20:
        return None
    levels = feature_series.astype(str)
    targets = target_series.astype(str)
    n_levels = levels.nunique()
    n_classes = targets.nunique()
    # Guards: a target with one class has nothing to leak; a high-cardinality
    # feature (>25 levels, or averaging under 10 rows per level) can look pure
    # by sparsity alone.
    if n_classes < 2 or n_classes > 20 or n_levels < 2 or n_levels > 25 or (n / n_levels) < 10:
        return None
    min_support = max(10, int(0.01 * n))
    deterministic_rows = 0
    examples = []
    for level, idx in pd.DataFrame({"f": levels, "t": targets}).groupby("f").groups.items():
        support = len(idx)
        if support < min_support:
            continue
        if targets.loc[idx].nunique() == 1:
            deterministic_rows += support
            examples.append({"level": str(level), "rows": int(support), "target": str(targets.loc[idx].iloc[0])})
    if not examples:
        return None
    examples.sort(key=lambda e: -e["rows"])
    return {
        "kind": "categoricalDeterminism",
        "value": round(deterministic_rows / n, 4),
        "detail": examples[:3],
    }


def leakage_signal(feature_series, target_series):
    """A real, generic leakage heuristic — not task-aware, not a
    substitute for a real domain review, but genuinely computed from the
    actual data: Pearson correlation for two real numeric columns
    (pandas' own .corr(), not hand-rolled), or a real exact-match rate
    otherwise (how often this feature's value literally equals the
    target's — catches a duplicated/derived target column, a common
    real leakage bug). Returns None when neither is meaningful (e.g.
    high-cardinality free text on one side)."""
    import pandas as pd
    try:
        if feature_series.dtype.kind in "iuf" and target_series.dtype.kind in "iuf":
            corr = feature_series.corr(target_series)
            if corr is None or pd.isna(corr):
                return None
            return {"kind": "correlation", "value": round(float(corr), 4)}
        else:
            aligned = pd.DataFrame({"f": feature_series, "t": target_series}).dropna()
            if len(aligned) == 0:
                return None
            match_rate = float((aligned["f"].astype(str) == aligned["t"].astype(str)).mean())
            if match_rate >= 0.5:
                return {"kind": "exactMatchRate", "value": round(match_rate, 4)}

            # Exact-match only catches a duplicated target. It misses the more
            # common real leak: a categorical feature whose LEVELS determine the
            # target without ever equalling it — e.g. Shipment_Status="Delayed"
            # being 100% of the delayed class while the target is 0/1. That
            # produces a perfect-looking f1 from a column that simply restates
            # the label. Measured here as the share of rows sitting in levels
            # that are wholly one target class.
            determinism = categorical_determinism(aligned["f"], aligned["t"])
            if determinism is not None:
                return determinism
            return {"kind": "exactMatchRate", "value": round(match_rate, 4)}


    except Exception:
        return None


def _is_nested(v):
    return isinstance(v, (list, dict))


def _as_text(v):
    """A list/dict cell as canonical JSON text: hashable, so unique counts,
    duplicate rows and leakage checks work, and equal values compare equal."""
    return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str) if _is_nested(v) else v


def main():
    try:
        req = json.loads(sys.stdin.read())
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"invalid input: {e}"}))
        return

    file_path = req.get("filePath")
    file_format = req.get("format")
    target_column = req.get("targetColumn")
    try:
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from dataset_loader import load_tabular, DatasetLoadError
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"dataset_loader not available: {e}"}))
        return

    try:
        df = load_tabular(file_path, file_format)
    except DatasetLoadError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"couldn't read '{file_path}': {e}"}))
        return

    # Chat/instruction JSONL carries lists and dicts in its cells (e.g. a
    # "messages" column of {role, content} turns). pandas can't hash those,
    # so profiling them directly crashed the whole upload. Profile their
    # JSON text instead; the preview rows below keep the real values.
    raw = df
    nested_cols = {c for c in df.columns if df[c].dtype == object and df[c].map(_is_nested).any()}
    if nested_cols:
        df = df.copy()
        for c in nested_cols:
            df[c] = df[c].map(_as_text)

    row_count = len(df)
    columns = []
    for col in df.columns:
        entry = {"name": col, "dtype": "nested (list/object)" if col in nested_cols else str(df[col].dtype)}
        if col in nested_cols:
            entry["nested"] = True
        entry.update(profile_column(df[col], row_count))
        if target_column and col != target_column and target_column in df.columns:
            signal = leakage_signal(df[col], df[target_column])
            if signal is not None:
                entry["leakageSignal"] = signal
        columns.append(entry)

    # Real sample rows (first 10) for the file-preview Compact tab — the
    # actual first rows of the real file, NaN -> None so it stays valid JSON.
    import pandas as _pd
    sample = raw.head(10)
    sample_rows = []
    for _, row in sample.iterrows():
        rec = {}
        for c in raw.columns:
            v = row[c]
            rec[str(c)] = v if _is_nested(v) else (None if _pd.isna(v) else (v.item() if hasattr(v, "item") else v))
        sample_rows.append(rec)

    def _default(o):
        return o.item() if hasattr(o, "item") else str(o)

    print(json.dumps({
        "ok": True,
        "rowCount": row_count,
        "columnCount": len(df.columns),
        "duplicateRowCount": int(df.duplicated().sum()),
        "columns": columns,
        "sampleColumns": [str(c) for c in df.columns],
        "sampleRows": sample_rows,
    }, default=_default))


if __name__ == "__main__":
    main()
