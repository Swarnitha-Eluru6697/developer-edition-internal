#!/usr/bin/env python3
"""Fine-tunes a Whisper speech-to-text model with LoRA on audio + transcripts.

Input (stdin, JSON):
  {"baseModelUri": "hf://openai/whisper-large-v3-turbo", "seed": 1, "datasetPath": "<local .zip>",
   "loraParams": {"rank", "alpha", "dropout", "learningRate", "epochs"},
   "speechConfig": {"maxSamples", "maxEvalSamples", "language", "batchSize"}}

The dataset is a ZIP holding the audio files plus metadata.csv with a file column
(path inside the ZIP) and a transcript column. Clips are resampled to 16 kHz mono;
clips over 30 s are skipped (Whisper's input window). Word error rate is measured
on the held-out clips before and after fine-tuning, so the run reports whether it
actually helped. Prints one JSON result on stdout; progress goes to stderr (MLLOG).
"""
import io, json, os, random, sys, tarfile, tempfile, time, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runlog import log

FILE_COLS = ("file", "path", "audio", "filename", "file_name")
TEXT_COLS = ("transcript", "text", "sentence", "transcription", "normalized_text")


def fail(msg):
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(0)


def word_error_rate(refs, hyps):
    """Corpus WER: word-level edit distance over total reference words."""
    import re
    norm = lambda s: re.sub(r"[^\w\s']", " ", s.lower()).split()
    errors = words = 0
    for r, h in zip(refs, hyps):
        r, h = norm(r), norm(h)
        d = list(range(len(h) + 1))
        for i in range(1, len(r) + 1):
            prev, d[0] = d[0], i
            for j in range(1, len(h) + 1):
                cur = d[j]
                d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
                prev = cur
        errors += d[len(h)]
        words += len(r)
    return errors / max(words, 1)


def load_clips(zip_path, np):
    import pandas as pd
    import soundfile as sf
    from scipy.signal import resample_poly
    z = zipfile.ZipFile(zip_path)
    meta_name = next((n for n in z.namelist() if n.lower().endswith("metadata.csv")), None)
    if not meta_name:
        fail("the ZIP has no metadata.csv — speech-to-text fine-tuning needs each audio file's transcript "
             "(metadata.csv with a file column and a transcript column)")
    meta = pd.read_csv(io.BytesIO(z.read(meta_name)))
    cols = {c.lower(): c for c in meta.columns}
    fcol = next((cols[c] for c in FILE_COLS if c in cols), None)
    tcol = next((cols[c] for c in TEXT_COLS if c in cols), None)
    if not fcol or not tcol:
        fail(f"metadata.csv needs a file column ({'/'.join(FILE_COLS)}) and a transcript column ({'/'.join(TEXT_COLS)}); found {list(meta.columns)}")
    base = os.path.dirname(meta_name)
    names = set(z.namelist())
    clips, skipped_long, missing = [], 0, 0
    for f, text in zip(meta[fcol].astype(str), meta[tcol].astype(str)):
        member = f if f in names else os.path.join(base, f) if os.path.join(base, f) in names else None
        if not member or not text.strip() or text == "nan":
            missing += 1
            continue
        audio, sr = sf.read(io.BytesIO(z.read(member)), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != 16000:
            from math import gcd
            g = gcd(sr, 16000)
            audio = resample_poly(audio, 16000 // g, sr // g).astype(np.float32)
        if len(audio) > 30 * 16000:
            skipped_long += 1
            continue
        clips.append((audio, text.strip()))
    return clips, missing, skipped_long


def main():
    req = json.loads(sys.stdin.read() or "{}")
    try:
        import numpy as np
        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
        from peft import LoraConfig, get_peft_model
    except Exception as e:
        fail(f"speech fine-tuning runtime unavailable: {e}")

    uri = req.get("baseModelUri") or ""
    hf_id = uri.split("://", 1)[1] if uri.startswith("hf://") else uri
    lora = req.get("loraParams") or {}
    cfg = req.get("speechConfig") or {}
    seed = int(req.get("seed") or 42)
    rank, alpha, dropout = int(lora.get("rank", 16)), int(lora.get("alpha", 32)), float(lora.get("dropout", 0.05))
    lr, epochs = float(lora.get("learningRate", "1e-4")), int(lora.get("epochs", 2))
    max_samples, max_eval = int(cfg.get("maxSamples", 400)), int(cfg.get("maxEvalSamples", 40))
    batch, language = int(cfg.get("batchSize", 4)), cfg.get("language") or "en"
    random.seed(seed); np.random.seed(seed); torch.manual_seed(seed)
    device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    start = time.time()

    clips, missing, skipped_long = load_clips(req["datasetPath"], np)
    if len(clips) < 10:
        fail(f"only {len(clips)} usable audio clips with transcripts — at least 10 are needed")
    random.shuffle(clips)
    total = len(clips)
    clips = clips[:max_samples]
    n_eval = max(2, round(len(clips) * 0.15))
    eval_clips, train_clips = clips[:n_eval], clips[n_eval:]
    log(f"Loaded {total} clips with transcripts" + (f" (used {len(clips)} — maxSamples {max_samples})" if total > len(clips) else "")
        + (f"; skipped {missing} without audio/transcript" if missing else "") + (f"; skipped {skipped_long} over 30 s" if skipped_long else ""),
        clips=total, used=len(clips), missing=missing, skipped_long=skipped_long)
    log(f"Split: {len(train_clips)} train / {len(eval_clips)} eval clips (random, seeded)", train=len(train_clips), eval=len(eval_clips))

    processor = AutoProcessor.from_pretrained(hf_id)
    processor.tokenizer.set_prefix_tokens(language=language, task="transcribe")
    model = AutoModelForSpeechSeq2Seq.from_pretrained(hf_id, torch_dtype=torch.float32).to(device)
    model.generation_config.language, model.generation_config.task = language, "transcribe"

    def features(batch_clips):
        return processor.feature_extractor([a for a, _ in batch_clips], sampling_rate=16000, return_tensors="pt").input_features.to(device)

    def transcribe(m, batch_clips):
        out = []
        m.eval()
        with torch.no_grad():
            for i in range(0, len(batch_clips), batch):
                ids = m.generate(features(batch_clips[i:i + batch]), language=language, task="transcribe", max_new_tokens=200)
                out += processor.batch_decode(ids, skip_special_tokens=True)
        return out

    eval_subset = eval_clips[:max_eval]
    refs = [t for _, t in eval_subset]
    base_wer = word_error_rate(refs, transcribe(model, eval_subset))
    log(f"Base model WER on {len(eval_subset)} eval clips: {base_wer:.3f}", base_wer=round(base_wer, 4))

    # LoRA on the decoder's attention only: the decoder maps the audio encoding to words,
    # which is what domain vocabulary and transcript style change. Leaving the 32-layer
    # encoder frozen means it runs forward-only — no back-propagation through a full 30 s
    # window per clip — which is what makes this practical on a laptop GPU.
    model = get_peft_model(model, LoraConfig(r=rank, lora_alpha=alpha, lora_dropout=dropout,
                                             target_modules=r".*decoder.*\.(q_proj|v_proj)"))
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total_params = sum(p.numel() for p in model.parameters())
    log(f"LoRA r={rank} α={alpha} on decoder q_proj/v_proj (encoder frozen) — {trainable:,} trainable of {total_params:,} parameters on {device}",
        trainable=trainable, total=total_params, device=device)

    start_id = model.config.decoder_start_token_id

    def labels_for(batch_clips):
        lab = processor.tokenizer([t for _, t in batch_clips], return_tensors="pt", padding=True)
        ids = lab.input_ids.masked_fill(lab.attention_mask.ne(1), -100)
        if (ids[:, 0] == start_id).all():  # the model prepends the start token itself
            ids = ids[:, 1:]
        return ids.to(device)

    encoder = model.get_encoder()

    def loss_on(batch_clips):
        with torch.no_grad():  # frozen encoder: forward only
            enc = encoder(features(batch_clips))
        return model(encoder_outputs=enc, labels=labels_for(batch_clips)).loss

    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=lr)
    train_loss = None
    steps_per_epoch = (len(train_clips) + batch - 1) // batch
    for ep in range(epochs):
        model.train()
        random.shuffle(train_clips)
        running = 0.0
        for s in range(steps_per_epoch):
            loss = loss_on(train_clips[s * batch:(s + 1) * batch])
            loss.backward()
            opt.step(); opt.zero_grad()
            running += float(loss)
            if (s + 1) % 10 == 0 or s + 1 == steps_per_epoch:
                log(f"Epoch {ep + 1}/{epochs} · step {s + 1}/{steps_per_epoch} · loss {running / (s + 1):.4f}",
                    epoch=ep + 1, step=s + 1, loss=round(running / (s + 1), 4))
        train_loss = running / max(steps_per_epoch, 1)

    model.eval()
    with torch.no_grad():
        eval_loss = sum(float(loss_on(eval_clips[i:i + batch])) for i in range(0, len(eval_clips), batch)) / max((len(eval_clips) + batch - 1) // batch, 1)
    tuned_wer = word_error_rate(refs, transcribe(model, eval_subset))
    log(f"Fine-tuned WER on the same {len(eval_subset)} eval clips: {tuned_wer:.3f} (base {base_wer:.3f})", wer=round(tuned_wer, 4))

    out_dir = tempfile.mkdtemp(prefix="speech-adapter-")
    adapter_dir = os.path.join(out_dir, "artifact")
    model.save_pretrained(adapter_dir)
    processor.save_pretrained(adapter_dir)
    json.dump({"task": "speech-to-text", "baseHfId": hf_id, "language": language, "method": "lora"},
              open(os.path.join(adapter_dir, "speech_meta.json"), "w"))
    tar_path = os.path.join(out_dir, "adapter.tar.gz")
    with tarfile.open(tar_path, "w:gz") as t:
        t.add(adapter_dir, arcname="artifact")

    metrics = {"wer": round(tuned_wer, 4), "baseWer": round(base_wer, 4), "evalLoss": round(eval_loss, 4), "trainLoss": round(train_loss, 4)}
    print(json.dumps({
        "ok": True, "measuredBy": f"transformers AutoModelForSpeechSeq2Seq + PEFT LoRA on {device}",
        "note": f"REAL Whisper LoRA fine-tune on {len(train_clips)} clips; WER measured on {len(eval_subset)} held-out clips before and after.",
        "method": "lora", "realDataset": True, "baseModelId": hf_id, "elapsedSeconds": round(time.time() - start, 2),
        "trainLoss": metrics["trainLoss"], "evalLoss": metrics["evalLoss"], "perplexity": None, "epochs": epochs,
        "trainSamples": len(train_clips), "evalSamples": len(eval_clips), "trainableParameters": trainable, "totalParameters": total_params,
        "trainableParameterPct": round(100 * trainable / total_params, 3), "finalMetrics": metrics,
        "split": {"train": len(train_clips), "validation": len(eval_clips), "test": 0, "unit": "clips", "method": "random 85 / 15 (seeded)", "evaluatedOn": "validation"},
        "effectiveHyperparams": {"rank": rank, "alpha": alpha, "dropout": dropout, "learningRate": lr, "epochs": epochs, "batchSize": batch,
                                 "maxSamples": max_samples, "maxEvalSamples": max_eval, "language": language, "targetModules": "decoder q_proj, v_proj (encoder frozen)"},
        "modelFilePath": tar_path, "modelFileSizeBytes": os.path.getsize(tar_path),
    }))


if __name__ == "__main__":
    main()
