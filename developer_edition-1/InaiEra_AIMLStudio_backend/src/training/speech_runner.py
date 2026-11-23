#!/usr/bin/env python3
"""Runs speech models for "Test this model" and serving.

Input (stdin, JSON):
  speech-to-text: {"task": "speech-to-text", "baseHfId": "openai/whisper-large-v3-turbo",
                   "adapterTarPath": "<optional fine-tuned adapter .tar.gz>",
                   "input": {"audioBase64": "<WAV/FLAC/OGG bytes>", "language": "en"}}
  text-to-speech: {"task": "text-to-speech", "baseHfId": "hexgrad/Kokoro-82M",
                   "input": {"text": "...", "voice": "default", "speed": 1.0}}
Speech-to-text splits long audio into 30 s windows (Whisper's input size) and joins
the text. Text-to-speech returns a 24 kHz WAV as base64. One JSON line on stdout.
"""
import base64, io, json, os, sys, tarfile

# The catalog's voice names → Kokoro's American-English voice packs.
KOKORO_VOICES = {"default": "af_heart", "warm": "af_bella", "narrator": "am_michael"}


def out(obj):
    print(json.dumps(obj))
    sys.exit(0)


def extract(tar_path):
    dest = tar_path + ".d"
    if not os.path.isdir(dest):
        with tarfile.open(tar_path) as t:
            t.extractall(dest)
    return os.path.join(dest, "artifact")


def speech_to_text(req):
    import numpy as np
    import soundfile as sf
    import torch
    from math import gcd
    from scipy.signal import resample_poly
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
    inp = req.get("input") or {}
    b64 = inp.get("audioBase64") or inp.get("audio_base64")
    if not b64:
        out({"ok": False, "error": "send the audio as audioBase64 (a WAV, FLAC or OGG file, base64-encoded)"})
    try:
        audio, sr = sf.read(io.BytesIO(base64.b64decode(b64.split(",")[-1])), dtype="float32")
    except Exception as e:
        out({"ok": False, "error": f"couldn't read the audio: {e}"})
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000:
        g = gcd(sr, 16000)
        audio = resample_poly(audio, 16000 // g, sr // g).astype(np.float32)
    device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    adapter = extract(req["adapterTarPath"]) if req.get("adapterTarPath") else None
    meta = json.load(open(os.path.join(adapter, "speech_meta.json"))) if adapter else {}
    base_id = meta.get("baseHfId") or req["baseHfId"]
    processor = AutoProcessor.from_pretrained(adapter or base_id)
    model = AutoModelForSpeechSeq2Seq.from_pretrained(base_id, torch_dtype=torch.float32)
    if adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, adapter)
    model = model.to(device).eval()
    language = inp.get("language") or meta.get("language") or "en"
    window = 30 * 16000
    texts = []
    with torch.no_grad():
        for i in range(0, len(audio), window):
            chunk = audio[i:i + window]
            if len(chunk) < 1600:  # under 0.1 s
                continue
            feats = processor.feature_extractor(chunk, sampling_rate=16000, return_tensors="pt").input_features.to(device)
            ids = model.generate(feats, language=language, task="transcribe", max_new_tokens=440)
            texts.append(processor.batch_decode(ids, skip_special_tokens=True)[0].strip())
    out({"ok": True, "prediction": {"transcript": " ".join(t for t in texts if t), "durationSeconds": round(len(audio) / 16000, 2),
                                    "language": language, "chunks": len(texts)},
         "modelClass": f"{'LoRA-fine-tuned ' if adapter else ''}{base_id} (AutoModelForSpeechSeq2Seq) on {device}"})


def text_to_speech(req):
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline
    inp = req.get("input") or {}
    text = (inp.get("text") or "").strip()
    if not text:
        out({"ok": False, "error": "send the text to speak as text"})
    if len(text) > 5000:
        out({"ok": False, "error": f"text is {len(text):,} characters — keep one request under 5,000 (about 5 minutes of speech)"})
    voice_name = str(inp.get("voice") or "default")
    voice = KOKORO_VOICES.get(voice_name, voice_name)
    speed = float(inp.get("speed") or 1.0)
    pipe = KPipeline(lang_code="a", repo_id=req.get("baseHfId") or "hexgrad/Kokoro-82M")
    parts = [a.numpy() if hasattr(a, "numpy") else np.asarray(a) for _, _, a in pipe(text, voice=voice, speed=speed)]
    if not parts:
        out({"ok": False, "error": "the model produced no audio for this text"})
    wav = np.concatenate(parts)
    buf = io.BytesIO()
    sf.write(buf, wav, 24000, format="WAV", subtype="PCM_16")
    out({"ok": True, "prediction": {"audioBase64": base64.b64encode(buf.getvalue()).decode(), "mimeType": "audio/wav",
                                    "sampleRate": 24000, "durationSeconds": round(len(wav) / 24000, 2), "voice": voice},
         "modelClass": f"Kokoro-82M (kokoro {__import__('kokoro').__version__}), voice {voice}"})


def main():
    req = json.loads(sys.stdin.read() or "{}")
    try:
        if req.get("task") == "speech-to-text":
            speech_to_text(req)
        elif req.get("task") == "text-to-speech":
            text_to_speech(req)
        else:
            out({"ok": False, "error": f"unknown speech task '{req.get('task')}'"})
    except SystemExit:
        raise
    except Exception as e:
        out({"ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
