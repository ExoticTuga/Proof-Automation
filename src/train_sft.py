#!/usr/bin/env python3
"""
train_sft.py -- full fine-tune of Qwen2.5-Coder-7B on Isabelle proof steps.

Launched by train.sbatch via torchrun; not normally run directly.

    torchrun --nproc_per_node=4 train_sft.py --data data/sft --out runs/qwen7b

WHAT THIS TRAINS
----------------
Causal language modelling, NOT sequence classification. The model reads a
prompt (theory context + proof state) and generates the next Isar command.

COMPLETION-ONLY LOSS
--------------------
Prompt tokens are labelled -100 so they contribute no loss. This matters more
here than in most fine-tunes: completions have a median length of 17
characters against a median prompt of 1172, so under a naive loss well over
95% of the gradient would come from reproducing proof states that were given
to the model as input. The model would learn to echo Isabelle's output rather
than to choose a tactic.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

import torch
from torch.utils.data import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
    set_seed,
)


class ProofStepDataset(Dataset):
    """Prompt/completion pairs with the prompt masked out of the loss."""

    def __init__(self, path: Path, tokenizer, max_len: int) -> None:
        self.rows = [json.loads(l) for l in path.open(encoding="utf-8")
                     if l.strip()]
        self.tok = tokenizer
        self.max_len = max_len

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, i: int) -> dict:
        r = self.rows[i]
        # add_special_tokens=False on both halves: we control the layout, and
        # a BOS inserted between prompt and completion would corrupt it.
        p = self.tok(r["prompt"], add_special_tokens=False)["input_ids"]
        c = self.tok(r["completion"], add_special_tokens=False)["input_ids"]
        c = c + [self.tok.eos_token_id]      # teach the model to stop

        # Truncate the PROMPT from the left if the pair is too long. The text
        # nearest the goal is the most relevant, and the completion must never
        # be truncated -- a clipped target teaches a malformed command.
        room = self.max_len - len(c)
        if room < 1:
            c = c[:self.max_len - 1] + [self.tok.eos_token_id]
            room = self.max_len - len(c)
        if len(p) > room:
            p = p[-room:]

        ids = p + c
        labels = [-100] * len(p) + c
        return {"input_ids": ids, "labels": labels,
                "attention_mask": [1] * len(ids)}


@dataclass
class PadCollator:
    """Right-pad a batch; label padding is -100 so it is ignored by the loss."""
    pad_id: int

    def __call__(self, feats: list[dict]) -> dict:
        n = max(len(f["input_ids"]) for f in feats)
        out = {"input_ids": [], "labels": [], "attention_mask": []}
        for f in feats:
            k = n - len(f["input_ids"])
            out["input_ids"].append(f["input_ids"] + [self.pad_id] * k)
            out["labels"].append(f["labels"] + [-100] * k)
            out["attention_mask"].append(f["attention_mask"] + [0] * k)
        return {k: torch.tensor(v, dtype=torch.long) for k, v in out.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen2.5-Coder-7B")
    ap.add_argument("--data", type=Path, default=Path("data/sft"))
    ap.add_argument("--out", type=Path, default=Path("runs/qwen7b"))
    ap.add_argument("--max-len", type=int, default=2048,
                    help="2048 covers ~95% of prompts (p95 is ~1800 tokens)")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=1e-5,
                    help="low: full fine-tune of a strong pretrained model")
    ap.add_argument("--per-device-batch", type=int, default=4)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--warmup-ratio", type=float, default=0.03)
    ap.add_argument("--save-steps", type=int, default=500)
    ap.add_argument("--eval-steps", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20260819)
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()

    set_seed(a.seed)
    is_main = int(os.environ.get("RANK", "0")) == 0

    tok = AutoTokenizer.from_pretrained(a.model, trust_remote_code=True)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token

    train = ProofStepDataset(a.data / "train.jsonl", tok, a.max_len)
    val_path = a.data / "val.jsonl"
    val = (ProofStepDataset(val_path, tok, a.max_len)
           if val_path.exists() and val_path.stat().st_size else None)
    if is_main:
        print(f"train {len(train)}  val {len(val) if val else 0}")

    model = AutoModelForCausalLM.from_pretrained(
        a.model,
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
        trust_remote_code=True,
    )
    model.config.use_cache = False        # incompatible with checkpointing

    world = int(os.environ.get("WORLD_SIZE", "1"))
    eff = a.per_device_batch * a.grad_accum * world
    if is_main:
        steps = math.ceil(len(train) / eff * a.epochs)
        print(f"effective batch {eff}, ~{steps} optimizer steps")

    # TrainingArguments has churned across transformers versions: arguments
    # get renamed (evaluation_strategy -> eval_strategy) and removed
    # (overwrite_output_dir). Build the full set, then keep only what the
    # installed signature accepts, reporting anything dropped rather than
    # failing on it.
    wanted = dict(
        output_dir=str(a.out),
        overwrite_output_dir=not a.resume,
        num_train_epochs=a.epochs,
        per_device_train_batch_size=a.per_device_batch,
        per_device_eval_batch_size=a.per_device_batch,
        gradient_accumulation_steps=a.grad_accum,
        learning_rate=a.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=a.warmup_ratio,
        weight_decay=0.01,
        max_grad_norm=1.0,
        bf16=True,
        logging_steps=25,
        # "none" rather than []: in transformers 5 an empty list suppresses
        # console logging as well as the external trackers, so a long run
        # produces no visible loss at all.
        disable_tqdm=False,
        save_steps=a.save_steps,
        save_total_limit=1,
        eval_strategy="steps" if val else "no",
        eval_steps=a.eval_steps if val else None,
        report_to="none",
        dataloader_num_workers=4,
        seed=a.seed,
        ddp_find_unused_parameters=False,
    )

    # FSDP shards parameters, gradients and optimizer state across GPUs. A 7B
    # full fine-tune needs ~112 GB for those three combined -- more than one
    # 80 GB card holds, but comfortable across four. It requires distributed
    # training, so a single-process run (the smoke test) uses plain
    # gradient checkpointing instead.
    if world > 1:
        wanted["fsdp"] = True
        wanted["fsdp_config"] = {
            "transformer_layer_cls_to_wrap": ["Qwen2DecoderLayer"],
            # preferred over gradient_checkpointing under FSDP: the latter
            # adds a redundant AllGather in the backward pass
            "activation_checkpointing": True,
            "use_orig_params": True,
        }
    else:
        wanted["gradient_checkpointing"] = True

    import inspect
    accepted = set(inspect.signature(TrainingArguments.__init__).parameters)
    # eval_strategy was called evaluation_strategy before transformers 4.41
    if "eval_strategy" not in accepted and "evaluation_strategy" in accepted:
        wanted["evaluation_strategy"] = wanted.pop("eval_strategy", "no")
    dropped = sorted(k for k in wanted if k not in accepted)
    kwargs = {k: v for k, v in wanted.items() if k in accepted}
    if is_main and dropped:
        print(f"note: TrainingArguments in this transformers version does not "
              f"accept {dropped}; ignoring")
    args = TrainingArguments(**kwargs)

    from transformers import TrainerCallback

    class LossToStdout(TrainerCallback):
        """Print each logged step to stdout.

        The Trainer's own console output goes through the logging module and
        is easy to lose in a batch job; this guarantees the loss reaches the
        job's .out file, which is the only way to monitor a multi-hour run.
        """

        def on_log(self, args_, state, control, logs=None, **kw):
            if not logs or not state.is_world_process_zero:
                return
            bits = " ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}"
                            for k, v in sorted(logs.items()))
            print(f"[step {state.global_step}] {bits}", flush=True)

    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train,
        eval_dataset=val,
        data_collator=PadCollator(tok.pad_token_id),
        callbacks=[LossToStdout()],
    )

    trainer.train(resume_from_checkpoint=a.resume or None)
    trainer.save_model(str(a.out / "final"))
    if is_main:
        tok.save_pretrained(str(a.out / "final"))
        print(f"saved to {a.out / 'final'}")


if __name__ == "__main__":
    main()
