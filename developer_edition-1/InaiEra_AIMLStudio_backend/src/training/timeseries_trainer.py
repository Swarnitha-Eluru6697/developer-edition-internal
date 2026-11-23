#!/usr/bin/env python3
"""
Real training for the 6 base models classical_trainer.py explicitly
doesn't cover — true time-series libraries with a genuinely different
fit shape (a univariate series, not X/y tabular data): Prophet, ARIMA,
SARIMA, auto-ARIMA, Exponential Smoothing, Holt-Winters.

Real dataset support: when a real timestamp/value contract is provided
(see timeseries_dataset_loader.py — timestampColumn, valueColumn,
optional entityColumn/entityValue for a multi-series dataset, an
optional declared frequency + missing-period policy), this loads and
fits on the ACTUAL real series — real chronological sort (never a
random split, which would leak future periods into training), a real
declared or inferred frequency, and a real held-out tail as the test
window. Falls back to the previous honestly-labeled synthetic series
(unchanged) when no real contract is given — most time-series base
models still won't have one.

Same honesty standard either way: a real fit against the actual named
library, real forecast-error metrics (MAE, RMSE, MAPE) measured against
a real held-out tail of the series, and a real saved model file.
"""
import sys
import json
import time
import warnings
import os
import tempfile
import contextlib

warnings.filterwarnings("ignore")


@contextlib.contextmanager
def _suppress_native_stdout():
    """See classical_trainer.py — some of these libraries (Prophet
    especially, via its cmdstanpy backend) write straight to the OS-level
    stdout file descriptor during fit, which would otherwise corrupt the
    JSON a caller parses."""
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


def make_series(seed, n=180):
    """A real synthetic daily series: trend + weekly seasonality + noise.
    Deterministic given the seed, same disclosed-synthetic-data pattern
    as classical_trainer.py."""
    import numpy as np
    rng = np.random.RandomState(seed)
    t = np.arange(n)
    trend = 0.15 * t
    seasonal = 8 * np.sin(2 * np.pi * t / 7)
    noise = rng.normal(0, 2.5, size=n)
    values = 50 + trend + seasonal + noise
    return values


def error_metrics(actual, predicted):
    import numpy as np
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    mae = float(np.mean(np.abs(actual - predicted)))
    rmse = float(np.sqrt(np.mean((actual - predicted) ** 2)))
    nonzero = actual != 0
    mape = float(np.mean(np.abs((actual[nonzero] - predicted[nonzero]) / actual[nonzero])) * 100) if nonzero.any() else None
    return {"mae": round(mae, 3), "rmse": round(rmse, 3), "mape": round(mape, 2) if mape is not None else None}


def main():
    try:
        req = json.loads(sys.stdin.read())
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"invalid input: {e}"}))
        return

    library = req.get("library", "")
    seed = int(req.get("seed", 0)) or 42
    dataset_path = req.get("datasetPath")
    dataset_format = req.get("datasetFormat")
    timestamp_column = req.get("timestampColumn")
    value_column = req.get("valueColumn")
    entity_column = req.get("entityColumn")
    entity_value = req.get("entityValue")
    frequency = req.get("frequency")
    timezone = req.get("timezone")
    missing_period_policy = req.get("missingPeriodPolicy") or "ffill"

    try:
        import numpy as np
        import joblib
        import pandas as pd
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"numpy/joblib/pandas not available: {e}"}))
        return

    real_dataset_note = None
    real_metadata = None
    if dataset_path:
        if not timestamp_column or not value_column:
            print(json.dumps({"ok": False, "error": "a real dataset was provided but timestampColumn and valueColumn are both required to resolve it as a real time series"}))
            return
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from timeseries_dataset_loader import load_real_series, TimeSeriesContractError
            real_series, real_metadata = load_real_series(
                dataset_path, dataset_format, timestamp_column, value_column,
                entity_column=entity_column, entity_value=entity_value,
                frequency=frequency, timezone=timezone, missing_period_policy=missing_period_policy,
            )
        except TimeSeriesContractError as e:
            print(json.dumps({"ok": False, "error": f"real time-series dataset contract failed: {e}"}))
            return
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__} loading real time-series dataset: {e}"}))
            return
        series = real_series.values
        series_index = real_series.index
        # A real, adaptive horizon — the previous fixed 14 assumed daily
        # synthetic data; real series come in any real frequency and
        # length. Real, bounded: at least 1 period, never more than 30,
        # and never more than a fifth of the real series so training
        # still has real data left to fit on.
        horizon = max(1, min(30, len(series) // 5))
        real_dataset_note = (
            f"REAL dataset ({real_metadata['realPeriods']} real periods, "
            f"{real_metadata['seriesStart']} to {real_metadata['seriesEnd']}"
            + (f", entity='{entity_value}'" if entity_value else "")
            + (f", {real_metadata['realGapsFilled']} real gap(s) filled via '{missing_period_policy}'" if real_metadata['realGapsFilled'] else "")
            + ") — real chronological split, never random."
        )
    else:
        series = make_series(seed)
        series_index = None
        horizon = 14  # real held-out test window — unchanged synthetic default

    train, test = series[:-horizon], series[-horizon:]
    start = time.time()

    try:
        effective_hp = None  # the settings this branch actually constructs the model with
        if library == "prophet":
            from prophet import Prophet
            if series_index is not None:
                dates = series_index[:-horizon]
            else:
                dates = pd.date_range("2025-01-01", periods=len(train), freq="D")
            df = pd.DataFrame({"ds": dates, "y": train})
            model = Prophet()
            effective_hp = {k: getattr(model, k) for k in ("changepoint_prior_scale", "seasonality_prior_scale", "holidays_prior_scale", "seasonality_mode", "changepoint_range")}
            with _suppress_native_stdout():
                model.fit(df)
            future = model.make_future_dataframe(periods=horizon, freq=(frequency or "D") if dataset_path else "D")
            forecast = model.predict(future)
            preds = forecast["yhat"].values[-horizon:]

        elif library == "arima":
            from statsmodels.tsa.arima.model import ARIMA
            with _suppress_native_stdout():
                model = ARIMA(train, order=(2, 1, 2)).fit()
            effective_hp = {"order": [2, 1, 2]}
            preds = model.forecast(steps=horizon)

        elif library == "sarima":
            from statsmodels.tsa.statespace.sarimax import SARIMAX
            with _suppress_native_stdout():
                model = SARIMAX(train, order=(1, 1, 1), seasonal_order=(1, 1, 1, 7)).fit(disp=False)
            effective_hp = {"order": [1, 1, 1], "seasonal_order": [1, 1, 1, 7]}
            preds = model.forecast(steps=horizon)

        elif library == "auto_arima":
            import pmdarima as pm
            with _suppress_native_stdout():
                model = pm.auto_arima(train, seasonal=True, m=7, suppress_warnings=True, error_action="ignore")
            effective_hp = {"seasonal": True, "m": 7, "chosen_order": list(model.order), "chosen_seasonal_order": list(model.seasonal_order)}
            preds = model.predict(n_periods=horizon)

        elif library == "exponential_smoothing":
            from statsmodels.tsa.holtwinters import ExponentialSmoothing
            with _suppress_native_stdout():
                model = ExponentialSmoothing(train, trend="add", seasonal=None).fit()
            effective_hp = {"trend": "add", "seasonal": None}
            preds = model.forecast(horizon)

        elif library == "holt_winters":
            from statsmodels.tsa.holtwinters import ExponentialSmoothing
            # Real seasonal period — 7 was a real assumption for the
            # synthetic weekly-seasonal series; for a real dataset, use
            # a period matched to the real declared/inferred frequency
            # where a sensible one exists (7 for daily/weekly-seasonal,
            # 12 for monthly/yearly-seasonal), falling back to 7 rather
            # than guessing something stranger.
            seasonal_periods = 7
            if dataset_path and real_metadata:
                freq_hint = (frequency or real_metadata.get("inferredFrequency") or "").upper()
                if freq_hint.startswith("M"):
                    seasonal_periods = 12
                elif freq_hint.startswith("D") or freq_hint.startswith("W"):
                    seasonal_periods = 7
            with _suppress_native_stdout():
                model = ExponentialSmoothing(train, trend="add", seasonal="add", seasonal_periods=seasonal_periods).fit()
            effective_hp = {"trend": "add", "seasonal": "add", "seasonal_periods": seasonal_periods}
            preds = model.forecast(horizon)

        else:
            print(json.dumps({"ok": False, "error": f"unrecognized library '{library}'"}))
            return

    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__} during real fit of {library}: {e}"}))
        return

    elapsed = time.time() - start
    metrics = error_metrics(test, preds)

    model_dir = tempfile.mkdtemp(prefix="ml-studio-timeseries-")
    model_path = os.path.join(model_dir, "model.joblib")
    try:
        joblib.dump({"model": model, "library": library, "timestampColumn": timestamp_column, "valueColumn": value_column, "frequency": frequency}, model_path)
    except Exception as e:
        # Prophet's fitted model wraps a cmdstanpy subprocess handle that
        # doesn't always pickle cleanly — a real, known limitation of
        # that library, not a defect in this script. Report honestly
        # rather than silently claim the artifact was saved.
        print(json.dumps({
            "ok": True, "measuredBy": f"{library} — real fit, real forecast, real error metrics",
            "split": {"train": len(train), "validation": 0, "test": len(test), "unit": "periods", "method": f"chronological — last {horizon} periods held out", "evaluatedOn": "test"},
            "effectiveHyperparams": effective_hp,
            "note": (real_dataset_note or "Synthetic data (no dataset file was provided for this training run)") + f" Model file could NOT be saved: {type(e).__name__}: {e}",
            "realDataset": bool(dataset_path),
            "elapsedSeconds": round(elapsed, 3), **metrics,
            "trainPoints": len(train), "testPoints": len(test), "horizonDays": horizon,
            "modelFilePath": None, "modelFileSizeBytes": 0,
        }))
        return

    print(json.dumps({
        "ok": True, "measuredBy": f"{library} — real fit, real forecast, real error metrics",
            "split": {"train": len(train), "validation": 0, "test": len(test), "unit": "periods", "method": f"chronological — last {horizon} periods held out", "evaluatedOn": "test"},
            "effectiveHyperparams": effective_hp,
        "note": real_dataset_note or "Synthetic data (no dataset file was provided for this training run); the FIT and forecast are real.",
        "realDataset": bool(dataset_path),
        "elapsedSeconds": round(elapsed, 3), **metrics,
        "trainPoints": len(train), "testPoints": len(test), "horizonDays": horizon,
        "modelFilePath": model_path, "modelFileSizeBytes": os.path.getsize(model_path),
    }))


if __name__ == "__main__":
    main()
