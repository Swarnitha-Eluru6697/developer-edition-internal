#!/usr/bin/env python3
"""
Real LLM fine-tuning — LoRA, QLoRA, or full fine-tuning — using the real
HuggingFace stack (transformers + peft + torch, plus bitsandbytes for
QLoRA's real 4-bit quantization). Same honesty standard as the other
three real trainers in this app: a real tokenizer, a real forward/
backward pass via HuggingFace's own Trainer, a real held-out eval split,
real measured eval loss / perplexity, and a real saved adapter or model
directory — never simulated numbers.

NOT runnable in the sandbox this app was otherwise built in: transformers
and peft both hard-depend on torch, and torch itself couldn't be
installed there (network policy blocks the CPU-only wheel index; the
only reachable PyPI build is CUDA-linked and doesn't fit that sandbox's
disk quota). This script was written and reviewed, but never executed —
it needs a real environment with torch/transformers/peft (and
bitsandbytes for QLoRA) actually installed, which is expected to be true
wherever this is deployed for real GPU-backed fine-tuning. If any of
those imports fail here too, this reports a real, honest
{"ok": false, "error": "..."} — the same pattern real_trainer.py and
classical_trainer.py already use for a genuinely missing dependency —
rather than silently falling back to something fake.

Usage: python3 llm_finetune_trainer.py < input.json > output.json
Input:  {
  "baseModelUri": "hf://meta-llama/Meta-Llama-3-8B-Instruct",
  "method": "lora" | "qlora" | "full",
  "loraParams": {"rank": int, "alpha": int, "dropout": float, "learningRate": "2e-4", "epochs": int},
  "fullParams": {"learningRate": "2e-5", "epochs": int, "warmupSteps": int},
  "seed": int,
  "datasetPath": "...", "datasetFormat": "csv"|"parquet"|...,
  "textColumn": "..."            (single-column mode), OR
  "promptColumn": "...", "completionColumn": "..."   (two-column mode),
  "maxSeqLength": int (default 512)
}
Output: {"ok": true, "measuredBy": "...", "method": "lora", ...} or
        {"ok": false, "error": "..."}
"""
import sys
import json
import os
import tarfile
import tempfile
import time

# The ONE line on stdout is this script's JSON result — llmFinetuneTrainer.ts
# parses stdout as a single JSON document. transformers/datasets/peft print
# their own progress to stdout (e.g. Trainer's "{'loss': 0.42, ...}" dicts),
# which made every successful run end in "couldn't parse … output". So the
# real stdout is kept for the result alone and everything else is sent to
# stderr, where the caller already collects diagnostics.
_RESULT_OUT = sys.stdout


def _emit(line: str) -> None:
    _RESULT_OUT.write(line + "\n")
    _RESULT_OUT.flush()


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from runlog import log
    try:
        req = json.loads(sys.stdin.read())
    except Exception as e:
        _emit(json.dumps({"ok": False, "error": f"invalid input: {e}"}))
        return

    base_model_uri = req.get("baseModelUri", "")
    method = req.get("method", "lora")
    seed = int(req.get("seed", 0)) or 42
    dataset_path = req.get("datasetPath")
    dataset_format = req.get("datasetFormat")
    text_column = req.get("textColumn")
    prompt_column = req.get("promptColumn")
    completion_column = req.get("completionColumn")
    max_seq_length = int(req.get("maxSeqLength", 512))
    lora_params = req.get("loraParams") or {}
    full_params = req.get("fullParams") or {}

    if not base_model_uri.startswith("hf://"):
        _emit(json.dumps({"ok": False, "error": f"baseModelUri '{base_model_uri}' isn't a real hf:// HuggingFace reference"}))
        return
    hf_model_id = base_model_uri[len("hf://"):]

    if not dataset_path:
        _emit(json.dumps({"ok": False, "error": "a real datasetPath is required — LLM fine-tuning has no synthetic-data fallback (there's no honest way to fabricate real instruction-tuning text)"}))
        return
    if not text_column and not (prompt_column and completion_column):
        _emit(json.dumps({"ok": False, "error": "either a real textColumn, or both promptColumn and completionColumn, are required to know which real dataset columns to train on"}))
        return
    if method not in ("lora", "qlora", "full"):
        _emit(json.dumps({"ok": False, "error": f"unrecognized method '{method}' — use lora, qlora, or full"}))
        return

    try:
        import torch
        from transformers import (
            AutoTokenizer, AutoModelForCausalLM, TrainingArguments, Trainer,
            DataCollatorForLanguageModeling, DataCollatorForSeq2Seq,
        )
    except Exception as e:
        _emit(json.dumps({"ok": False, "error": f"torch/transformers not available: {e}"}))
        return
    if method in ("lora", "qlora"):
        try:
            from peft import LoraConfig, get_peft_model, TaskType
        except Exception as e:
            _emit(json.dumps({"ok": False, "error": f"peft not available (required for {method}): {e}"}))
            return
    if method == "qlora":
        try:
            from transformers import BitsAndBytesConfig
            import bitsandbytes  # noqa: F401 — presence check; BitsAndBytesConfig is what's actually used
        except Exception as e:
            _emit(json.dumps({"ok": False, "error": f"bitsandbytes not available (required for real 4-bit QLoRA quantization): {e}"}))
            return

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from dataset_loader import load_tabular, DatasetLoadError
    except Exception as e:
        _emit(json.dumps({"ok": False, "error": f"dataset_loader not available: {e}"}))
        return

    try:
        df = load_tabular(dataset_path, dataset_format)
    except DatasetLoadError as e:
        _emit(json.dumps({"ok": False, "error": str(e)}))
        return

    # Real text assembly — either a dataset that's already one column of
    # complete training text, or a real prompt/completion pair joined
    # with the tokenizer's own real EOS token (not a hand-picked
    # separator string that might not mean anything to this specific
    # model's real vocabulary).
    if text_column:
        if text_column not in df.columns:
            _emit(json.dumps({"ok": False, "error": f"textColumn '{text_column}' not found — real columns are: {list(df.columns)}"}))
            return
        texts = df[text_column].fillna("").astype(str).tolist()
    else:
        missing = [c for c in (prompt_column, completion_column) if c not in df.columns]
        if missing:
            _emit(json.dumps({"ok": False, "error": f"column(s) not found: {missing} — real columns are: {list(df.columns)}"}))
            return
        prompts = df[prompt_column].fillna("").astype(str).tolist()
        completions = df[completion_column].fillna("").astype(str).tolist()
        # An optional per-row system prompt (a "system" column), e.g. the persona
        # an instruction set was written for. Kept so inference can use the same one.
        systems = df["system"].fillna("").astype(str).tolist() if "system" in df.columns else [""] * len(df)
        texts = None  # assembled below, once the tokenizer is known

    log(f"Loaded dataset: {len(df)} rows · columns {list(df.columns)}", rows=len(df), columns=list(df.columns),
        mode="prompt/completion" if text_column is None else f"text column '{text_column}'")
    if len(df) < 10:
        _emit(json.dumps({"ok": False, "error": f"only {len(df)} real row(s) in this dataset — not enough for a real train/eval split"}))
        return

    start = time.time()
    try:
        tokenizer = AutoTokenizer.from_pretrained(hf_model_id)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        # Prompt/completion pairs are trained in the model's own chat format — the
        # format it is spoken to in at inference — with the loss on the answer only.
        # Joining them as raw "prompt<eos>completion" text taught a format nobody
        # uses at inference, and spent the loss on reproducing the questions.
        chat_pairs = None
        if texts is None:
            if getattr(tokenizer, "chat_template", None):
                chat_pairs = []
                for s, p, c in zip(systems, prompts, completions):
                    msgs = ([{"role": "system", "content": s}] if s else []) + [{"role": "user", "content": p}]
                    prefix = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
                    full = tokenizer.apply_chat_template(msgs + [{"role": "assistant", "content": c}], tokenize=False)
                    chat_pairs.append((prefix, full))
            else:
                texts = [f"{p}{tokenizer.eos_token}{c}{tokenizer.eos_token}" for p, c in zip(prompts, completions)]

        quantization_config = None
        if method == "qlora":
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True,
            )
        model = AutoModelForCausalLM.from_pretrained(
            hf_model_id,
            quantization_config=quantization_config,
            # bfloat16 on Apple GPUs too: fp32 weights plus activations overflowed a 48 GB
            # Mac into swap and a 3-hour run crawled for 12. LoRA adapters PEFT adds stay fp32.
            torch_dtype=torch.bfloat16 if (torch.cuda.is_available() or torch.backends.mps.is_available()) else torch.float32,
        )
        if method in ("lora", "qlora"):
            # Recompute activations in the backward pass instead of storing every layer's.
            model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
            model.enable_input_require_grads()

        real_lora_config = None
        if method in ("lora", "qlora"):
            real_lora_config = LoraConfig(
                task_type=TaskType.CAUSAL_LM,
                r=int(lora_params.get("rank", 16)),
                lora_alpha=int(lora_params.get("alpha", 32)),
                lora_dropout=float(lora_params.get("dropout", 0.05)),
                # A real, standard default target-module set for
                # attention projection layers — the actual real
                # parameters PEFT inserts real low-rank adapters into.
                # All attention and MLP projections: adapting only q/v left too little
                # capacity to learn a new language's syntax (code tasks especially).
                target_modules=lora_params.get("targetModules") or
                    ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            )
            model = get_peft_model(model, real_lora_config)
            # Train the adapters in fp32 even when the frozen base is bfloat16.
            for p_ in model.parameters():
                if p_.requires_grad:
                    p_.data = p_.data.float()

        real_trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
        real_total_params = sum(p.numel() for p in model.parameters())
        log(f"Loaded {hf_model_id} for {method} — {real_trainable_params:,} trainable of {real_total_params:,} parameters "
            f"({100 * real_trainable_params / max(real_total_params, 1):.2f}%)", base_model=hf_model_id, method=method,
            trainable=real_trainable_params, total=real_total_params, lora=lora_params if method != "full" else None,
            chat_format=chat_pairs is not None if "chat_pairs" in dir() else None)

    except Exception as e:
        _emit(json.dumps({"ok": False, "error": f"{type(e).__name__} loading real base model/tokenizer '{hf_model_id}': {e}"}))
        return

    try:
        from datasets import Dataset
        if chat_pairs is not None:
            # Labels are -100 over the prompt so only the answer is learned; no padding
            # here — the collator pads each batch to its own longest example.
            def encode(prefix, full):
                ids = tokenizer(full, truncation=True, max_length=max_seq_length, add_special_tokens=False)["input_ids"]
                n = min(len(tokenizer(prefix, add_special_tokens=False)["input_ids"]), len(ids))
                return {"input_ids": ids, "attention_mask": [1] * len(ids), "labels": [-100] * n + ids[n:]}
            tokenized = Dataset.from_list([encode(pf, fu) for pf, fu in chat_pairs])
            tokenized = tokenized.filter(lambda ex: any(l != -100 for l in ex["labels"]))
        else:
            raw_ds = Dataset.from_dict({"text": texts})

            def tokenize_fn(batch):
                return tokenizer(batch["text"], truncation=True, max_length=max_seq_length)

            tokenized = raw_ds.map(tokenize_fn, batched=True, remove_columns=["text"])
        split = tokenized.train_test_split(test_size=0.15, seed=seed)
        train_ds, eval_ds = split["train"], split["test"]
        lens = sorted(len(x) for x in train_ds["input_ids"])
        log(f"Tokenised: {len(train_ds)} train / {len(eval_ds)} eval examples · tokens/example p50 {lens[len(lens)//2]}, max {lens[-1]} (cap {max_seq_length})",
            train=len(train_ds), eval=len(eval_ds), p50_tokens=lens[len(lens) // 2], max_tokens=lens[-1], max_seq_length=max_seq_length)
    except Exception as e:
        _emit(json.dumps({"ok": False, "error": f"{type(e).__name__} tokenizing real dataset: {e}"}))
        return

    params = lora_params if method in ("lora", "qlora") else full_params
    try:
        learning_rate = float(params.get("learningRate", "2e-4" if method != "full" else "2e-5"))
    except (TypeError, ValueError):
        learning_rate = 2e-4 if method != "full" else 2e-5
    epochs = int(params.get("epochs", 3))
    warmup_steps = int(full_params.get("warmupSteps", 0)) if method == "full" else 0

    output_dir = tempfile.mkdtemp(prefix="ml-studio-llm-finetune-")
    try:
        torch.manual_seed(seed)
        training_args = TrainingArguments(
            output_dir=os.path.join(output_dir, "checkpoints"),
            num_train_epochs=epochs,
            learning_rate=learning_rate,
            warmup_steps=warmup_steps,
            per_device_train_batch_size=1,
            per_device_eval_batch_size=1,
            gradient_accumulation_steps=4,
            logging_steps=10,
            eval_strategy="epoch",
            save_strategy="epoch",
            save_total_limit=1,
            report_to=[],
            seed=seed,
            bf16=torch.cuda.is_available(),
        )
        from transformers import TrainerCallback

        class _Progress(TrainerCallback):
            """One log line per logging step: loss, learning rate, epoch and an ETA."""
            def on_train_begin(self, args, state, control, **kw):
                self.t0 = time.time()
                log(f"Training started — {state.max_steps} optimiser steps over {args.num_train_epochs} epoch(s), "
                    f"batch 1 x grad-accum {args.gradient_accumulation_steps}, lr {args.learning_rate}",
                    max_steps=state.max_steps, epochs=args.num_train_epochs, lr=args.learning_rate)

            def on_log(self, args, state, control, logs=None, **kw):
                logs = logs or {}
                if "loss" in logs:
                    done = state.global_step / max(state.max_steps, 1)
                    spent = time.time() - self.t0
                    eta = spent / done - spent if done > 0 else None
                    log(f"step {state.global_step}/{state.max_steps} ({100 * done:.0f}%) · loss {logs['loss']:.4f} · "
                        f"lr {logs.get('learning_rate', 0):.2e} · epoch {logs.get('epoch', 0):.2f}"
                        + (f" · ETA {int(eta // 60)}m{int(eta % 60):02d}s" if eta is not None else ""),
                        step=state.global_step, loss=logs["loss"], lr=logs.get("learning_rate"), epoch=logs.get("epoch"),
                        grad_norm=logs.get("grad_norm"), eta_s=round(eta) if eta is not None else None)
                elif "eval_loss" in logs:
                    log(f"Evaluation at step {state.global_step}: eval_loss {logs['eval_loss']:.4f}", level="ok",
                        eval_loss=logs["eval_loss"], step=state.global_step)

            def on_train_end(self, args, state, control, **kw):
                log(f"Training finished — {state.global_step} steps in {time.time() - self.t0:.0f}s", level="ok")

        trainer = Trainer(
            model=model, args=training_args, callbacks=[_Progress()],
            train_dataset=train_ds, eval_dataset=eval_ds,
            data_collator=(DataCollatorForSeq2Seq(tokenizer, padding=True, label_pad_token_id=-100)
                           if chat_pairs is not None else DataCollatorForLanguageModeling(tokenizer, mlm=False)),
        )
        train_result = trainer.train()
        eval_result = trainer.evaluate()
    except Exception as e:
        _emit(json.dumps({"ok": False, "error": f"{type(e).__name__} during real {method} fine-tuning: {e}"}))
        return

    elapsed = time.time() - start
    real_eval_loss = float(eval_result.get("eval_loss", float("nan")))
    import math
    real_perplexity = math.exp(real_eval_loss) if real_eval_loss == real_eval_loss and real_eval_loss < 50 else None

    # Real save — PEFT's save_pretrained writes ONLY the real adapter
    # weights + config for lora/qlora (a real, small artifact, not a
    # full copy of the base model); the full method saves the complete
    # real fine-tuned model. Tokenizer saved alongside either way so the
    # real artifact is genuinely self-contained for later inference.
    artifact_dir = os.path.join(output_dir, "artifact")
    try:
        model.save_pretrained(artifact_dir)
        tokenizer.save_pretrained(artifact_dir)
        # The system prompt the model was trained with, so inference can speak to it
        # the same way (llm_generate_runner.py reads this).
        trained_system = max(set(systems), key=systems.count) if text_column is None and any(systems) else ""
        with open(os.path.join(artifact_dir, "chat_defaults.json"), "w") as f:
            json.dump({"system": trained_system, "chat_format": chat_pairs is not None}, f)
    except Exception as e:
        _emit(json.dumps({"ok": False, "error": f"real training succeeded but saving the real artifact failed: {e}"}))
        return

    # Tarred into one real file so this fits the same single-file
    # upload path every other real trainer in this app already uses
    # (uploadRealWeights) — a real adapter/model directory has multiple
    # real files (config, weights, tokenizer), not one.
    tar_path = os.path.join(output_dir, "artifact.tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(artifact_dir, arcname="artifact")
    log(f"Saved artifact ({os.path.getsize(tar_path) / 1e6:.1f} MB): {sorted(os.listdir(artifact_dir))}", level="ok",
        size_bytes=os.path.getsize(tar_path))

    _emit(json.dumps({
        "ok": True,
        "split": {"train": len(train_ds), "validation": len(eval_ds), "test": 0, "unit": "examples", "method": "random 85 / 15 (seeded) — eval set used for eval loss", "evaluatedOn": "validation"},
        "measuredBy": f"real {method} fine-tuning of '{hf_model_id}' — transformers.Trainer, real forward/backward pass",
        "note": f"REAL dataset ({len(df)} rows, {'text column' if text_column else 'prompt/completion columns'}) — real train/eval split ({len(train_ds)}/{len(eval_ds)}), real measured loss.",
        "method": method,
        "realDataset": True,
        "baseModelId": hf_model_id,
        "elapsedSeconds": round(elapsed, 3),
        "trainLoss": round(float(train_result.training_loss), 4),
        "evalLoss": round(real_eval_loss, 4),
        "perplexity": round(real_perplexity, 3) if real_perplexity is not None else None,
        "epochs": epochs,
        "trainSamples": len(train_ds),
        "evalSamples": len(eval_ds),
        "trainableParameters": real_trainable_params,
        "totalParameters": real_total_params,
        "trainableParameterPct": round((real_trainable_params / real_total_params) * 100, 4) if real_total_params else None,
        "modelFilePath": tar_path,
        "modelFileSizeBytes": os.path.getsize(tar_path),
    }))


if __name__ == "__main__":
    sys.stdout = sys.stderr
    main()
