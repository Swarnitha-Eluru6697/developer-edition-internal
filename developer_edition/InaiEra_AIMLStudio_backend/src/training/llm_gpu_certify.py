#!/usr/bin/env python3
"""Production certification harness for the ML Studio LLM fine-tuning runtime.

Usage:
  python3 llm_gpu_certify.py --preflight
  python3 llm_gpu_certify.py --config /path/to/cert.json

A full cert config uses the same fields as llm_finetune_trainer.py and MUST
point at a real locally-accessible dataset and a small accessible hf:// model.
The full mode executes a real fine-tune, untars the produced artifact, reloads
it with Transformers/PEFT, and performs one generation. Exit 0 means the CUDA
runtime + training + artifact reload path is certified on THIS image/node.
"""
import argparse, json, os, subprocess, sys, tarfile, tempfile

REQUIRED = ["torch", "transformers", "peft", "accelerate"]

def preflight(require_qlora=False):
    versions = {}
    for name in REQUIRED + (["bitsandbytes"] if require_qlora else []):
        mod = __import__(name)
        versions[name] = getattr(mod, "__version__", "unknown")
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available to torch; GPU certification cannot pass on this node")
    versions["cuda"] = torch.version.cuda
    versions["gpu"] = torch.cuda.get_device_name(0)
    versions["gpu_count"] = torch.cuda.device_count()
    return versions

def full_cert(config_path):
    cfg = json.load(open(config_path, "r", encoding="utf-8"))
    method = cfg.get("method", "lora")
    env = preflight(require_qlora=(method == "qlora"))
    trainer = os.path.join(os.path.dirname(__file__), "llm_finetune_trainer.py")
    proc = subprocess.run([sys.executable, trainer], input=json.dumps(cfg), text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"fine-tune subprocess failed: {proc.stderr[-1500:]}")
    out = json.loads(proc.stdout.strip())
    if not out.get("ok"):
        raise RuntimeError(out.get("error", "fine-tune returned ok=false"))
    artifact = out["modelFilePath"]
    base_uri = cfg["baseModelUri"]
    model_id = base_uri[5:] if base_uri.startswith("hf://") else base_uri
    with tempfile.TemporaryDirectory(prefix="mlstudio-llm-cert-") as td:
        with tarfile.open(artifact, "r:gz") as tar:
            tar.extractall(td)
        art = os.path.join(td, "artifact")
        from transformers import AutoTokenizer, AutoModelForCausalLM
        import torch
        tok = AutoTokenizer.from_pretrained(art)
        if method == "full":
            model = AutoModelForCausalLM.from_pretrained(art, torch_dtype="auto", device_map="auto")
        else:
            from peft import PeftModel
            base = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype="auto", device_map="auto")
            model = PeftModel.from_pretrained(base, art)
        inputs = tok("Certification prompt", return_tensors="pt")
        inputs = {k: v.to(next(model.parameters()).device) for k, v in inputs.items()}
        with torch.no_grad():
            generated = model.generate(**inputs, max_new_tokens=4)
        if generated.shape[-1] <= inputs["input_ids"].shape[-1]:
            raise RuntimeError("artifact reloaded, but generation produced no new token")
    return {"ok": True, "certified": True, "method": method, "runtime": env,
            "trainLoss": out.get("trainLoss"), "evalLoss": out.get("evalLoss"),
            "artifactReload": True, "generation": True}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--preflight", action="store_true"); ap.add_argument("--config")
    a = ap.parse_args()
    try:
        result = {"ok": True, "certified": False, "preflight": preflight(False)} if a.preflight else full_cert(a.config)
        print(json.dumps(result)); return 0
    except Exception as e:
        print(json.dumps({"ok": False, "certified": False, "error": str(e)})); return 2
if __name__ == "__main__": raise SystemExit(main())
