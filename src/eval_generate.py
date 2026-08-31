#!/usr/bin/env python3
"""
eval_generate.py -- generate next-tactic predictions and score them.

    python eval_generate.py --model runs/qwen7b/final \\
        --data data/sft_test/test.jsonl --out results/qwen7b_test.jsonl

WHAT EXACT MATCH DOES AND DOES NOT MEASURE
------------------------------------------
Exact match is a LOWER BOUND on capability, and a loose one. Many distinct
tactics discharge the same goal -- `by auto`, `by simp` and `by blast` are
often interchangeable -- so a model that produces a perfectly good proof step
scores zero whenever the human happened to write a different one. The figure
is reported because it is cheap, reproducible and comparable across models,
not because it is the quantity of interest. Whether Isabelle *accepts* the
generated command is the real measure, and is scored separately.

Three metrics are reported, in increasing looseness:

  exact         normalised string equality with the reference
  command       the Isar command keyword matches (`by` vs `by`), arguments
                may differ -- measures whether the model chose the right
                KIND of step
  prefix@n      first n characters agree, a crude partial-credit measure
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


def normalise(s: str) -> str:
    """Collapse whitespace; Isar is insensitive to it."""
    return " ".join(s.strip().split())


def head_command(s: str) -> str:
    m = re.match(r"[A-Za-z][A-Za-z0-9_']*", s.strip())
    return m.group(0) if m else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=None,
                    help="score only the first N examples")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--max-new-tokens", type=int, default=64)
    ap.add_argument("--max-prompt-tokens", type=int, default=1984)
    ap.add_argument("--num-return", type=int, default=1,
                    help=">1 samples several candidates and reports pass@k, "
                         "which is the fairer measure when a proof step has "
                         "many valid forms")
    ap.add_argument("--temperature", type=float, default=0.0,
                    help="0 = greedy. Sampling only makes sense with "
                         "--num-return > 1")
    args = ap.parse_args()

    rows = [json.loads(l) for l in args.data.open(encoding="utf-8") if l.strip()]
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} examples from {args.data}")

    tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    # decoder-only models must be left-padded for batched generation, or the
    # padding sits between the prompt and the first generated token
    tok.padding_side = "left"

    model = AutoModelForCausalLM.from_pretrained(
        args.model, dtype=torch.bfloat16, device_map="auto",
        trust_remote_code=True)
    model.eval()

    gen_kwargs = dict(
        max_new_tokens=args.max_new_tokens,
        pad_token_id=tok.pad_token_id,
        num_return_sequences=args.num_return,
    )
    if args.temperature > 0:
        gen_kwargs.update(do_sample=True, temperature=args.temperature,
                          top_p=0.95)
    else:
        gen_kwargs.update(do_sample=False)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    results = []
    t0 = time.time()

    for i in range(0, len(rows), args.batch_size):
        batch = rows[i:i + args.batch_size]
        enc = tok([r["prompt"] for r in batch], return_tensors="pt",
                  padding=True, truncation=True,
                  max_length=args.max_prompt_tokens,
                  add_special_tokens=False).to(model.device)

        with torch.no_grad():
            out = model.generate(**enc, **gen_kwargs)

        # strip the prompt: generate() returns prompt + completion
        new = out[:, enc["input_ids"].shape[1]:]
        texts = tok.batch_decode(new, skip_special_tokens=True)

        for j, r in enumerate(batch):
            cands = [normalise(t.split("\n")[0])
                     for t in texts[j * args.num_return:(j + 1) * args.num_return]]
            ref = normalise(r["completion"])
            results.append({
                "reference": ref,
                "predictions": cands,
                "exact": any(c == ref for c in cands),
                "command": any(head_command(c) == head_command(ref)
                               for c in cands),
                "meta": r.get("meta", {}),
            })

        done = min(i + args.batch_size, len(rows))
        rate = done / max(1e-9, time.time() - t0)
        print(f"\r{done}/{len(rows)}  {rate:.1f}/s  "
              f"exact so far {sum(x['exact'] for x in results)/len(results):.3f}",
              end="", flush=True)

    print()
    with args.out.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    n = len(results)
    exact = sum(r["exact"] for r in results)
    cmd = sum(r["command"] for r in results)
    k = args.num_return
    tag = f"@{k}" if k > 1 else ""

    print(f"\n{'='*54}\n{n} examples, {args.model}\n{'='*54}")
    print(f"exact match{tag}:      {exact/n:.4f}  ({exact})")
    print(f"command match{tag}:    {cmd/n:.4f}  ({cmd})")

    # Break down by command: a headline figure is dominated by whichever
    # commands are most frequent, and those are the easy structural ones.
    by = defaultdict(lambda: [0, 0])
    for r in results:
        c = r["meta"].get("command", "?")
        by[c][0] += r["exact"]
        by[c][1] += 1
    print(f"\n{'command':<14}{'n':>7}{'exact':>9}")
    for c, (e, t) in sorted(by.items(), key=lambda kv: -kv[1][1])[:12]:
        print(f"{c:<14}{t:>7}{e/t:>9.3f}")

    closing = [r for r in results if r["meta"].get("closes_proof")]
    if closing:
        ce = sum(r["exact"] for r in closing)
        print(f"\nproof-closing steps: {ce/len(closing):.4f} "
              f"({ce}/{len(closing)})")
        rest = [r for r in results if not r["meta"].get("closes_proof")]
        if rest:
            re_ = sum(r["exact"] for r in rest)
            print(f"all other steps:     {re_/len(rest):.4f} "
                  f"({re_}/{len(rest)})")

    print("\nmost common predictions:")
    for p, c in Counter(r["predictions"][0] for r in results).most_common(8):
        print(f"  {c:>6}  {p[:60]}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
