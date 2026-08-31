#!/usr/bin/env python3
"""
prepare_dataset.py -- turn harvested proof states into fine-tuning pairs.

    python prepare_dataset.py data/train_merged/states.jsonl --out data/sft

Produces train.jsonl and val.jsonl with fields:

    prompt      theory text so far + the current proof state
    completion  the next Isar command (what the model must generate)
    meta        file, line, command, remaining -- for analysis, not training

WHY states.jsonl AND NOT transitions.jsonl
------------------------------------------
A transition pairs two consecutive recorded states, so it only exists where a
*following* state was recorded. Isabelle emits no state at the position that
closes a proof, so the closing command (`done`, `qed`, a final `by ...`) never
appears as a transition target -- and the model would never learn to finish a
proof. Deriving the target from each state row's `continuation` keeps those
rows: the last row of a proof has state "No subgoals!" and continuation "done".

FILTERING
---------
Dropped: rows outside any proof, rows without a prefix, rows whose state or
output contains Isabelle error text, and rows from files containing `sorry` or
`oops` (admitted proofs are not valid targets). Duplicate (state, completion)
pairs are collapsed -- boilerplate like `by simp` on an identical goal recurs
thousands of times and would otherwise dominate the loss.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from afp_harvest import command_spans  # noqa: E402

try:  # only in afp_harvest versions that read theory-declared commands
    from afp_harvest import load_keyword_declarations  # noqa: E402
except ImportError:
    load_keyword_declarations = None

ERROR_MARKERS = (
    "Failed to", "Undefined", "Type unification failed", "Inner syntax error",
    "Outer syntax error", "Malformed", "Illegal application", "*** ",
    "exception", "Timeout", "Step error", "Bad ",
)

PROMPT_TEMPLATE = (
    "(* Isabelle/HOL proof. Given the theory context and the current proof "
    "state, give the next Isar command. *)\n\n"
    "### Context\n{prefix}\n\n"
    "### Proof state\n{state}\n\n"
    "### Next command\n"
)


def first_command(text: str) -> str | None:
    """The first complete Isar command in a continuation."""
    if not text.strip():
        return None
    spans = command_spans(text)
    if not spans:
        return None
    _, s, e = spans[0]
    cmd = " ".join(text[s:e].split())
    return cmd or None


def truncate_prefix(prefix: str, max_chars: int) -> str:
    """Keep the END of the prefix -- the text nearest the goal is what matters.

    Truncation is aligned to a line boundary so the context does not begin
    mid-expression, and an elision marker makes the cut visible to the model.
    """
    if len(prefix) <= max_chars:
        return prefix
    cut = prefix[-max_chars:]
    nl = cut.find("\n")
    if 0 <= nl < 200:
        cut = cut[nl + 1:]
    return "(* ... *)\n" + cut


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("states", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--afp", type=Path, default=None,
                    help="afp/thys, so theory-declared commands are recognised")
    ap.add_argument("--max-prefix-chars", type=int, default=6000,
                    help="~2000 tokens; the prefix is left-truncated")
    ap.add_argument("--max-state-chars", type=int, default=4000)
    ap.add_argument("--max-completion-chars", type=int, default=600)
    ap.add_argument("--val-frac", type=float, default=0.02,
                    help="held out from TRAINING entries, to watch for "
                         "overfitting during the run. This is not the "
                         "evaluation set -- that is a separate set of entries.")
    ap.add_argument("--keep-sorry", action="store_true")
    ap.add_argument("--no-dedupe", action="store_true")
    ap.add_argument("--seed", type=int, default=20260818)
    a = ap.parse_args()

    if a.afp and load_keyword_declarations is not None:
        load_keyword_declarations(a.afp.expanduser().resolve())
    elif a.afp:
        print("note: this afp_harvest.py predates theory-declared command "
              "support, so --afp has no effect. Harmless here: it only "
              "affects which rows were harvested, not this conversion.")

    rows = [json.loads(l) for l in a.states.open(encoding="utf-8") if l.strip()]
    print(f"{len(rows)} harvested states")

    # files containing sorry/oops: those proofs are admitted, not proved
    bad_files: set[str] = set()
    if not a.keep_sorry:
        for f in {r["file"] for r in rows}:
            try:
                if re.search(r"\b(sorry|oops)\b",
                             Path(f).read_text(encoding="utf-8", errors="replace")):
                    bad_files.add(f)
            except OSError:
                pass
        print(f"{len(bad_files)} files contain sorry/oops")

    drop = Counter()
    pairs: list[dict] = []
    for r in rows:
        if not r.get("in_proof"):
            drop["outside a proof"] += 1
            continue
        if not r.get("prefix"):
            drop["no prefix"] += 1
            continue
        if r["file"] in bad_files:
            drop["sorry/oops file"] += 1
            continue
        blob = r.get("state", "") + r.get("output", "")
        if any(m in blob for m in ERROR_MARKERS):
            drop["error text"] += 1
            continue
        target = first_command(r.get("continuation", ""))
        if not target:
            drop["no next command"] += 1
            continue
        if len(target) > a.max_completion_chars:
            drop["completion too long"] += 1
            continue
        if not r.get("state", "").strip():
            drop["empty state"] += 1
            continue

        pairs.append({
            "prompt": PROMPT_TEMPLATE.format(
                prefix=truncate_prefix(r["prefix"], a.max_prefix_chars),
                state=r["state"][:a.max_state_chars],
            ),
            "completion": target,
            "meta": {
                "file": r["file"],
                "entry": Path(r["file"]).parent.name,
                "line": r["line"],
                "command": r["command"],
                "remaining": r.get("remaining", -1),
                "closes_proof": r.get("remaining", -1) <= 1,
            },
        })

    print(f"\n{len(pairs)} pairs before dedupe")
    for k, v in drop.most_common():
        print(f"  dropped {v:>7}  {k}")

    if not a.no_dedupe:
        seen: set[tuple] = set()
        deduped = []
        for p in pairs:
            key = (p["prompt"][-1500:], p["completion"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(p)
        print(f"\ndeduped: {len(pairs)} -> {len(deduped)} "
              f"({len(pairs) - len(deduped)} removed)")
        pairs = deduped

    # split by ENTRY, so the val set measures generalisation rather than
    # memorisation of a theory the model has otherwise seen
    rng = random.Random(a.seed)
    entries = sorted({p["meta"]["entry"] for p in pairs})
    rng.shuffle(entries)
    n_val = max(1, int(len(entries) * a.val_frac))
    if n_val >= len(entries):          # tiny corpora: never empty the train set
        n_val = 0
    val_entries = set(entries[:n_val])
    train = [p for p in pairs if p["meta"]["entry"] not in val_entries]
    val = [p for p in pairs if p["meta"]["entry"] in val_entries]

    a.out.mkdir(parents=True, exist_ok=True)
    for name, data in (("train", train), ("val", val)):
        path = a.out / f"{name}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for p in data:
                fh.write(json.dumps(p, ensure_ascii=False) + "\n")
        print(f"wrote {len(data):>7} -> {path}")

    print(f"\nentries: {len(entries)} train / {len(val_entries)} val")
    plen = sorted(len(p["prompt"]) for p in pairs)
    clen = sorted(len(p["completion"]) for p in pairs)
    print(f"prompt chars:     median {plen[len(plen)//2]}  "
          f"p95 {plen[int(len(plen)*0.95)]}  max {plen[-1]}")
    print(f"completion chars: median {clen[len(clen)//2]}  "
          f"p95 {clen[int(len(clen)*0.95)]}  max {clen[-1]}")
    print(f"closing steps:    "
          f"{sum(1 for p in pairs if p['meta']['closes_proof'])} "
          f"({100.0*sum(1 for p in pairs if p['meta']['closes_proof'])/max(1,len(pairs)):.0f}%)")
    print("\nmost common completions:")
    for c, n in Counter(p["completion"] for p in pairs).most_common(8):
        print(f"  {n:>6}  {c[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
