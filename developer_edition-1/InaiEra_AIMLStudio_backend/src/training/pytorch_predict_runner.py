#!/usr/bin/env python3
"""
Real inference against a trained PyTorch artifact.

Input: JSON on stdin, {"modelFilePath": ".../model-pytorch.tar.gz", "input": {...}}.
Output: one JSON line, the same shape predict_runner.py returns.

Why this exists separately: pytorch_trainer.py packages its result as a
tar.gz holding `model.pt` (state_dict + metadata) and `preprocessor.joblib`,
but the prediction path only ever knew how to `joblib.load()` a single file.
Pointing it at the tarball failed with "pop from empty list" — pickle choking
on gzip bytes — and every deep-learning model silently fell back to the
development stand-in. So DL models were trained for real and then never
actually served.

A state_dict is only weights: the architecture has to be rebuilt before it can
be loaded. It is rebuilt HERE from the metadata the trainer saved, matching
pytorch_trainer.py's own constructors layer for layer — if the two ever drift
apart, load_state_dict raises on the shape mismatch rather than silently
producing wrong numbers.

Architectures that genuinely cannot answer a single-row request (sequence
models need a window of history, forecasters need a horizon) say so instead of
inventing a padded input.
"""
import sys
import json
import os
import tarfile
import tempfile
import shutil


def _make_mlp(nn, input_dim, hidden, output_dim):
    """Identical to pytorch_trainer._make_mlp — same layer order, so the saved
    state_dict keys line up exactly."""
    layers = []
    d = input_dim
    for h in hidden:
        layers += [nn.Linear(d, int(h)), nn.ReLU(), nn.BatchNorm1d(int(h)), nn.Dropout(0.1)]
        d = int(h)
    layers.append(nn.Linear(d, output_dim))
    return nn.Sequential(*layers)


def _build_autoencoder(nn, input_dim, hidden, bottleneck):
    class AE(nn.Module):
        def __init__(self):
            super().__init__()
            self.encoder = nn.Sequential(nn.Linear(input_dim, hidden), nn.ReLU(), nn.Linear(hidden, bottleneck))
            self.decoder = nn.Sequential(nn.Linear(bottleneck, hidden), nn.ReLU(), nn.Linear(hidden, input_dim))

        def forward(self, x):
            return self.decoder(self.encoder(x))

    return AE()


def _output_dim_from_state(state_dict):
    """Rows of the last Linear weight = the model's output width.

    Read from the weights rather than the metadata because the MLP metadata
    records hiddenLayers and inputDim but not the class count, and guessing it
    would be the one number capable of silently changing every prediction.
    """
    last = None
    for key, tensor in state_dict.items():
        if key.endswith(".weight") and hasattr(tensor, "shape") and len(tensor.shape) == 2:
            last = tensor
    return int(last.shape[0]) if last is not None else None


def _rebuild(model_id, meta, state_dict, torch):
    from torch import nn

    task = meta.get("task")
    input_dim = int(meta.get("inputDim") or 0)

    if "hiddenLayers" in meta:
        out_dim = _output_dim_from_state(state_dict) or (1 if task == "regression" else 2)
        return _make_mlp(nn, input_dim, meta["hiddenLayers"], out_dim), "mlp"

    if "bottleneckDim" in meta:
        return _build_autoencoder(nn, input_dim, int(meta["hiddenDim"]), int(meta["bottleneckDim"])), "autoencoder"

    if "sequenceLength" in meta:
        raise NotImplementedError(
            f"'{model_id}' is a sequence model trained on windows of {meta['sequenceLength']} consecutive rows. "
            "A single-row request cannot be scored against it — that needs a real history window, not a padded row."
        )

    if task == "forecasting":
        raise NotImplementedError(
            f"'{model_id}' is a forecaster. It produces a horizon from a time-indexed series, "
            "so it has no single-row prediction to return."
        )

    raise NotImplementedError(
        f"no inference path is implemented for '{model_id}' (architecture: {meta.get('architecture', 'unknown')})"
    )


def main():
    try:
        req = json.loads(sys.stdin.read())
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"invalid input: {e}"}))
        return

    bundle_path = req.get("modelFilePath")
    input_obj = req.get("input", {}) or {}

    try:
        import torch
        import numpy as np
        import joblib
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"torch/joblib not available: {e}"}))
        return

    workdir = tempfile.mkdtemp(prefix="ml-studio-torch-predict-")
    try:
        try:
            with tarfile.open(bundle_path, "r:gz") as tar:
                # Only the two members the trainer writes, and never a path
                # that escapes the extraction directory.
                for member in tar.getmembers():
                    if member.name not in ("model.pt", "preprocessor.joblib"):
                        continue
                    tar.extract(member, workdir)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"couldn't open the PyTorch artifact bundle: {e}"}))
            return

        checkpoint_path = os.path.join(workdir, "model.pt")
        prep_path = os.path.join(workdir, "preprocessor.joblib")
        if not os.path.exists(checkpoint_path):
            print(json.dumps({"ok": False, "error": "artifact bundle has no model.pt"}))
            return

        try:
            checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"couldn't load model.pt: {e}"}))
            return

        state_dict = checkpoint.get("state_dict")
        meta = checkpoint.get("metadata") or {}
        if state_dict is None:
            print(json.dumps({"ok": False, "error": "model.pt has no state_dict"}))
            return

        prep_bundle = {}
        if os.path.exists(prep_path):
            try:
                prep_bundle = joblib.load(prep_path)
            except Exception:
                prep_bundle = {}
        preprocessor = prep_bundle.get("preprocessor")
        label_encoder = prep_bundle.get("labelEncoder")
        # Regression targets are standardised during training, so the raw head
        # output is a z-score. Without inverting it a salary model answers
        # "3.06" instead of "60,240". Absent for artifacts trained before the
        # scaler existed — those genuinely have no way back to real units, and
        # that is reported rather than papered over.
        target_scaler = prep_bundle.get("targetScaler")

        model_id = meta.get("modelId", "unknown")
        try:
            model, kind = _rebuild(model_id, meta, state_dict, torch)
        except NotImplementedError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"couldn't rebuild the architecture: {e}"}))
            return

        try:
            model.load_state_dict(state_dict)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"saved weights don't match the rebuilt architecture: {e}"}))
            return
        model.eval()

        # --- the real input contract, enforced not guessed --------------------
        shape_note = None
        if preprocessor is not None:
            try:
                import pandas as pd
                feature_names = list(preprocessor.feature_names_in_)
                missing = [c for c in feature_names if c not in input_obj]
                if missing:
                    print(json.dumps({"ok": False, "error": f"input is missing required real column(s) this model was trained on: {missing}. Expected all of: {feature_names}"}))
                    return
                row = pd.DataFrame([{c: input_obj[c] for c in feature_names}])
                X = preprocessor.transform(row)
                if hasattr(X, "toarray"):
                    X = X.toarray()
                X = np.asarray(X, dtype="float32")
                shape_note = f"real preprocessing pipeline applied — {len(feature_names)} real training column(s): {feature_names}"
            except Exception as e:
                print(json.dumps({"ok": False, "error": f"{type(e).__name__} applying the real saved preprocessor: {e}"}))
                return
        else:
            values = [float(v) for v in input_obj.values() if isinstance(v, (int, float))]
            expected = int(meta.get("inputDim") or len(values))
            if len(values) < expected:
                values += [0.0] * (expected - len(values))
                shape_note = f"no preprocessor saved with this artifact — {len(input_obj)} numeric field(s) padded to the model's {expected} inputs"
            elif len(values) > expected:
                values = values[:expected]
                shape_note = f"no preprocessor saved with this artifact — input truncated to the model's {expected} inputs"
            X = np.asarray([values], dtype="float32")

        tensor = torch.from_numpy(X)

        try:
            with torch.no_grad():
                out = model(tensor)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__} during real forward pass: {e}"}))
            return

        result = {}
        if kind == "autoencoder":
            # An autoencoder's "prediction" is its reconstruction error, scored
            # against the threshold the trainer computed from the training set.
            recon = out.numpy()
            error = float(((recon - X) ** 2).mean())
            threshold = meta.get("threshold")
            result = {"prediction": error, "reconstructionError": round(error, 6)}
            if isinstance(threshold, (int, float)):
                result["threshold"] = round(float(threshold), 6)
                result["anomaly"] = bool(error > threshold)
        elif meta.get("task") == "regression" or out.shape[-1] == 1:
            raw = float(out.reshape(-1)[0])
            if target_scaler is not None:
                try:
                    raw = float(target_scaler.inverse_transform(np.asarray([[raw]], dtype="float64")).reshape(-1)[0])
                except Exception as e:
                    print(json.dumps({"ok": False, "error": f"couldn't invert the target scaler: {e}"}))
                    return
                result = {"prediction": raw}
            else:
                result = {
                    "prediction": raw,
                    "unitWarning": "This artifact carries no target scaler, so the value is the model's raw output and may not be in the target's real units. Retrain to get a calibrated prediction.",
                }
        else:
            probs = torch.softmax(out, dim=-1).numpy()[0]
            idx = int(probs.argmax())
            label = idx
            if label_encoder is not None:
                try:
                    decoded = label_encoder.inverse_transform([idx])[0]
                    label = decoded.item() if hasattr(decoded, "item") else decoded
                except Exception:
                    pass
            result = {"prediction": label, "probability": round(float(probs[idx]), 4)}

        print(json.dumps({
            "ok": True,
            **result,
            "shapeNote": shape_note,
            "modelClass": meta.get("architecture", type(model).__name__),
            "realPreprocessing": preprocessor is not None,
        }))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
