"""Generate text from a fine-tuned LLM artifact — what test-predict runs for an
LLM version, deployed or not.

Input (stdin, JSON): {"modelFilePath": "<local .tar.gz>", "input": <request body>}
  input may be {"messages": [{"role", "content"}, ...]}, {"prompt": "..."}, or a
  plain string; optional "max_new_tokens" (default 1024, max 4096), "temperature" (default
  0 = greedy, reproducible), "repetition_penalty" (default 1.1).
Output (stdout, one JSON line): {"ok": true, "prediction": "<text>", "modelClass", "shapeNote"}
  or {"ok": false, "error": "..."}.

The artifact is the trainer's artifact.tar.gz: a directory "artifact/" holding
either a PEFT adapter (adapter_config.json names the base model) or a full model.
The base model loads from the local Hugging Face cache. Library logging goes to
stderr so stdout carries only the result line — the trainer learned that the hard
way when progress dicts on stdout made a finished run unparseable.
"""
import json
import os
import sys
import tarfile

_RESULT_OUT = sys.stdout


def _emit(obj):
    _RESULT_OUT.write(json.dumps(obj) + "\n")
    _RESULT_OUT.flush()


def _extract(tar_path: str) -> str:
    """Extract once next to the cached tarball; reuse on later calls."""
    dest = tar_path + ".d"
    marker = os.path.join(dest, ".complete")
    if not os.path.exists(marker):
        os.makedirs(dest, exist_ok=True)
        with tarfile.open(tar_path, "r:gz") as tar:
            for m in tar.getmembers():
                target = os.path.realpath(os.path.join(dest, m.name))
                if not target.startswith(os.path.realpath(dest) + os.sep):
                    raise ValueError(f"unsafe path in artifact: {m.name}")
            tar.extractall(dest)
        open(marker, "w").close()
    art = os.path.join(dest, "artifact")
    return art if os.path.isdir(art) else dest


def _prompt_and_messages(inp):
    if isinstance(inp, str):
        return inp, None
    if isinstance(inp, dict):
        if isinstance(inp.get("messages"), list) and inp["messages"]:
            return None, inp["messages"]
        for k in ("prompt", "text", "input"):
            if isinstance(inp.get(k), str) and inp[k].strip():
                return inp[k], None
    raise ValueError('send {"messages": [{"role": "user", "content": "..."}]} or {"prompt": "..."}')


def main():
    try:
        req = json.loads(sys.stdin.read())
        path, inp = req["modelFilePath"], req.get("input") or {}
        prompt, messages = _prompt_and_messages(inp)
        # 200 tokens cut code answers off mid-query; 1024 fits a full query plus its explanation.
        max_new = max(16, min(4096, int((inp.get("max_new_tokens") if isinstance(inp, dict) else None) or 1024)))
        temperature = float((inp.get("temperature") if isinstance(inp, dict) else None) or 0)
        # A mild penalty keeps a small model out of repetition loops (an option list
        # repeated until the token limit); 1.0 turns it off.
        rep_penalty = float((inp.get("repetition_penalty") if isinstance(inp, dict) else None) or 1.1)
    except Exception as e:
        return _emit({"ok": False, "error": f"invalid input: {e}"})

    sys.stdout = sys.stderr  # everything below may print; keep it off the result stream
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except Exception as e:
        return _emit({"ok": False, "error": f"torch/transformers not available: {e}"})

    try:
        art = _extract(path)
        device = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
        dtype = torch.float16 if device in ("mps", "cuda") else torch.float32
        adapter_cfg = os.path.join(art, "adapter_config.json")
        tokenizer = AutoTokenizer.from_pretrained(art)
        if os.path.exists(adapter_cfg):
            from peft import PeftModel
            base_id = json.load(open(adapter_cfg))["base_model_name_or_path"]
            base = AutoModelForCausalLM.from_pretrained(base_id, torch_dtype=dtype)
            model = PeftModel.from_pretrained(base, art)
            model_class = f"PeftModel({type(base).__name__}, base {base_id})"
        else:
            model = AutoModelForCausalLM.from_pretrained(art, torch_dtype=dtype)
            model_class = type(model).__name__
        model.to(device).eval()
    except Exception as e:
        return _emit({"ok": False, "error": f"{type(e).__name__} loading the fine-tuned model: {e}"})

    try:
        # Speak to it the way it was trained: the trainer records the system prompt
        # its examples used; add it when the caller didn't send one.
        defaults_path = os.path.join(art, "chat_defaults.json")
        trained_system = json.load(open(defaults_path)).get("system") if os.path.exists(defaults_path) else ""
        if messages is not None and trained_system and not any(m.get("role") == "system" for m in messages):
            messages = [{"role": "system", "content": trained_system}] + messages
        if messages is not None and getattr(tokenizer, "chat_template", None):
            text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        elif messages is not None:
            text = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages) + "\nassistant:"
        else:
            text = prompt
        enc = tokenizer(text, return_tensors="pt").to(device)
        gen = dict(max_new_tokens=max_new, pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
                   repetition_penalty=rep_penalty)
        gen.update(dict(do_sample=True, temperature=temperature) if temperature > 0 else dict(do_sample=False))
        with torch.no_grad():
            out = model.generate(**enc, **gen)
        reply = tokenizer.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True).strip()
    except Exception as e:
        return _emit({"ok": False, "error": f"{type(e).__name__} during generation: {e}"})

    generated = len(out[0]) - enc["input_ids"].shape[1]
    hit_limit = generated >= max_new and int(out[0][-1]) != tokenizer.eos_token_id
    _emit({"ok": True, "prediction": reply, "modelClass": model_class, "truncated": hit_limit, "maxNewTokens": max_new,
           "shapeNote": f"generated {generated} tokens on {device}"
                        + (f" — stopped at the {max_new}-token limit; raise max_new_tokens for the rest" if hit_limit else "")})


if __name__ == "__main__":
    main()
