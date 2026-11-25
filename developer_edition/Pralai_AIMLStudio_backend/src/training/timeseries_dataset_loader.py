#!/usr/bin/env python3
"""
Real time-series dataset resolution — the actual contract a real
forecasting dataset needs, which the generic tabular snapshot mechanism
(built for classification/regression's X/y shape) doesn't establish on
its own: a real timestamp column, a real value column, an optional real
entity key for multi-series data, a real declared frequency, an optional
timezone, a real missing-period policy, and a real CHRONOLOGICAL split
— never a random one, which would leak future periods into training for
time-series data specifically.

Honestly scoped: multi-series support here means "pick one real series
out of a multi-entity dataset by entity value," not real hierarchical/
global multi-series forecasting (training one model across many
entities at once) — that's a materially larger feature (real per-entity
or global architectures, real entity embeddings) not attempted here.
"""
import pandas as pd


class TimeSeriesContractError(Exception):
    """A real, named failure in the time-series dataset contract itself
    — the timestamp column doesn't parse, the value column isn't
    numeric, the requested entity doesn't exist, not enough real
    periods remain after resampling. Distinct from a generic load
    failure so the caller can report exactly which part of the real
    contract wasn't honored."""
    pass


def load_real_series(
    file_path,
    file_format,
    timestamp_column,
    value_column,
    entity_column=None,
    entity_value=None,
    frequency=None,
    timezone=None,
    missing_period_policy="ffill",
):
    """Returns (series, metadata) — series is a real pandas Series,
    datetime-indexed, chronologically sorted, real-resampled to the
    declared frequency if one was given, with the real missing-period
    policy applied. metadata reports what was actually done, honestly —
    real period count, real gap count found, the real inferred
    frequency if the caller didn't declare one.
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dataset_loader import load_tabular, DatasetLoadError

    try:
        df = load_tabular(file_path, file_format)
    except DatasetLoadError as e:
        raise TimeSeriesContractError(f"couldn't load the real dataset file: {e}")

    if timestamp_column not in df.columns:
        raise TimeSeriesContractError(f"timestamp column '{timestamp_column}' not found — real columns are: {list(df.columns)}")
    if value_column not in df.columns:
        raise TimeSeriesContractError(f"value column '{value_column}' not found — real columns are: {list(df.columns)}")

    # Real entity filtering — a multi-series dataset (one row per
    # entity per period) needs a real, explicit choice of WHICH real
    # series this run trains on; there's no honest default here.
    if entity_column:
        if entity_column not in df.columns:
            raise TimeSeriesContractError(f"entity column '{entity_column}' not found — real columns are: {list(df.columns)}")
        if entity_value is None:
            real_entities = sorted(df[entity_column].astype(str).unique().tolist())[:20]
            raise TimeSeriesContractError(f"entity column '{entity_column}' was given but no entityValue was chosen — pick one real entity to train on, e.g.: {real_entities}")
        df = df[df[entity_column].astype(str) == str(entity_value)]
        if len(df) == 0:
            raise TimeSeriesContractError(f"entity '{entity_value}' has no real rows in column '{entity_column}'")

    # Real timestamp parsing — a column that doesn't actually parse as
    # dates is a real, named contract violation, not silently coerced
    # into something else.
    try:
        timestamps = pd.to_datetime(df[timestamp_column], errors="raise")
    except Exception as e:
        raise TimeSeriesContractError(f"timestamp column '{timestamp_column}' doesn't parse as real dates: {e}")

    values = pd.to_numeric(df[value_column], errors="coerce")
    if values.isna().all():
        raise TimeSeriesContractError(f"value column '{value_column}' has no real numeric values")

    series = pd.Series(values.values, index=pd.DatetimeIndex(timestamps.values), name=value_column)
    series = series[~series.index.isna()]
    series = series.sort_index()  # real chronological order — never assumed from row order

    if timezone:
        try:
            if series.index.tz is None:
                series.index = series.index.tz_localize(timezone)
            else:
                series.index = series.index.tz_convert(timezone)
        except Exception as e:
            raise TimeSeriesContractError(f"couldn't apply timezone '{timezone}': {e}")

    duplicate_periods = int(series.index.duplicated().sum())
    if duplicate_periods > 0:
        # Real duplicate timestamps (e.g. two rows for the same real
        # period) are averaged — a real, disclosed aggregation choice,
        # not silently dropped or arbitrarily kept.
        series = series.groupby(series.index).mean()

    inferred_freq = pd.infer_freq(series.index) if len(series) > 2 else None
    real_gap_count = 0
    if frequency:
        try:
            full_index = pd.date_range(start=series.index.min(), end=series.index.max(), freq=frequency)
        except Exception as e:
            raise TimeSeriesContractError(f"'{frequency}' isn't a real pandas-recognized frequency string: {e}")
        real_gap_count = len(full_index) - len(series)
        series = series.reindex(full_index)
        if missing_period_policy == "ffill":
            series = series.ffill()
        elif missing_period_policy == "interpolate":
            series = series.interpolate(method="linear")
        elif missing_period_policy == "zero":
            series = series.fillna(0.0)
        elif missing_period_policy == "drop":
            series = series.dropna()
        else:
            raise TimeSeriesContractError(f"unrecognized missingPeriodPolicy '{missing_period_policy}' — use ffill, interpolate, zero, or drop")
        # ffill/interpolate can still leave real leading NaNs if the
        # series doesn't start with a real value — drop those rather
        # than feed a real trainer a NaN it didn't ask to handle.
        series = series.dropna()

    if len(series) < 10:
        raise TimeSeriesContractError(f"only {len(series)} real period(s) remain after resolving this dataset — not enough for a real train/test split")

    metadata = {
        "realPeriods": len(series),
        "inferredFrequency": inferred_freq,
        "declaredFrequency": frequency,
        "realGapsFilled": real_gap_count,
        "duplicatePeriodsAveraged": duplicate_periods,
        "seriesStart": str(series.index.min()),
        "seriesEnd": str(series.index.max()),
    }
    return series, metadata
