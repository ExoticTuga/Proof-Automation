# Fine-tuning a Language Model for Isabelle/HOL Proof Automation

MSc dissertation project. Extracts a corpus of proof states from the Archive
of Formal Proofs, fine-tunes Qwen2.5-Coder-7B to predict the next Isar
command, and evaluates whether Isabelle accepts what the model writes.

**Start at [`index.html`](index.html)** for an annotated guide to every file.

## Results

All figures are on held-out AFP entries absent from training.

| | Base Qwen2.5-Coder-7B | Fine-tuned |
|---|---|---|
| Proofs accepted by Isabelle | 0 / 19 | **3 / 19** |
| Exact match (70,242 steps) | 5.1% | **35.9%** |
| Command match (70,242 steps) | 12.6% | **68.2%** |

Completion is reported on the 19 theorems **both** models attempted. The
fine-tuned run additionally proved a twentieth theorem that the base run never
reached, because the theory failed to elaborate within its warm-up budget;
counting it would compare different denominators.
`compare_completion.py` checks this. Use `--targets-in` to fix an identical
theorem list across runs.

## The problem

The AFP contains proof *scripts* but not proof *states*. A `.thy` file records
the tactics a human wrote; the intermediate goals those tactics act upon exist
only inside Isabelle while the file is being checked, and are never written to
disk. Since the training signal maps a proof state to the tactic applied to
it, the states must be recovered by re-executing the corpus under
instrumentation.

`afp_harvest.py` does this by driving Isabelle over the Language Server
Protocol, emulating an interactive editor: it moves a cursor through each
theory file and records the proof state Isabelle reports at each position.

```
   theory file            cursor position          Isabelle's response
   ─────────────────      ───────────────          ───────────────────
   lemma rev_rev: …       end of `lemma …`     →   goal (1 subgoal):
                                                    1. rev (rev xs) = xs
     apply (induct xs)    end of `apply …`     →   goal (2 subgoals): …
     apply simp           end of `apply simp`  →   No subgoals!
   done                   end of `done`        →   (no output)
```

## Reproducing

### Requirements

- Python 3.12, `pip install -r requirements.txt`
- [isabelle-emacs](https://github.com/m-fleury/isabelle-emacs), revision
  `Isabelle2025-2-vsce` — the upstream Isabelle VSCode server does not send the
  PIDE notifications this depends on
- The AFP snapshot `2026-06-29` from [isa-afp.org](https://www.isa-afp.org/)
- For training and evaluation: an 80 GB GPU (3 for training)

### Offline checks — no Isabelle, no GPU

```bash
python src/afp_harvest.py --self-test
```

32 assertions over the Isar lexer, cursor positioning, proof segmentation and
training-pair construction. Each extraction defect encountered during
development is covered by a regression case.

```bash
python src/afp_harvest.py --afp <afp>/thys --show-blocks > blocks.txt
grep -c "NOT PROBED" blocks.txt     # expect 0
```

Checks the central structural invariant — *every proof block must terminate at
a position some cursor visits* — across the entire archive. Verified on all
249,241 proof blocks in 6,857 theory files.

### Full pipeline

```bash
# 1. partition the corpus by entry, stratified, fixed seed
python src/split_entries.py --afp <afp>/thys --test-frac 0.2 \
       --only-parents HOL --out-dir split_hol

# 2. extract proof states (Slurm job array; ~9 h across 14 tasks)
sbatch slurm/harvest_array.sbatch
python src/merge_shards.py data/train --out data/train_merged \
       --expect-entries split_hol/train_entries.txt \
       --test-entries  split_hol/test_entries.txt
python src/validate_dataset.py data/train_merged

# 3. build training pairs
python src/prepare_dataset.py data/train_merged/states.jsonl --out data/sft

# 4. fine-tune (3 x A100 80GB, ~52 h)
sbatch slurm/train.sbatch

# 5. evaluate
sbatch slurm/eval.sbatch                          # per-step
MODEL=runs/qwen7b/final sbatch slurm/completion.sbatch   # proof completion
```

## Notes on the implementation

Three points cost more to discover than the code suggests. The third is a
limitation of this version rather than a fix.

**The state panel is a separate LSP channel from the output panel.**
`PIDE/dynamic_output` carries proof hints and completed theorems, and is
*empty at exactly the tactic applications that matter*. The proof state comes
from `PIDE/state_output`, which must be instantiated with an explicit
`PIDE/state_init` request before it emits anything.

**HTML output is mandatory in this Isabelle revision.** The plain-text
rendering path attaches a decoration object the JSON encoder cannot serialise
(`Bad JSON value: …Lambda`), so no state is emitted at all. The extraction
requests HTML and converts it back, which is lossless: the pretty-printer
breaks lines before rendering, so the HTML's text content is the plain-text
state.

**Isabelle theories can define their own commands — a known limitation.** A
theory header may declare `keywords "sepref_definition" :: thy_goal`, making
that a goal-opening command for every importing theory. No fixed keyword list
can anticipate these, and an unrecognised goal-opener loses an entire theory's
surrounding context.

This was identified after extraction had completed and is **not** implemented
in the version here. Re-extracting and retraining was not feasible in the time
available, so the code shipped is the code that produced the reported results.
The effect is bounded: approximately 3.5% of extracted records lack prefix and
continuation, concentrated in entries built on proof frameworks. Proof states
themselves are unaffected. Implementing it is the first item of future work.

## Repository layout

```
src/                pipeline: extraction, dataset prep, training, evaluation
tools/              standalone diagnostics
slurm/              Slurm job scripts (cluster-specific; edit paths at the top)
split_hol/          the train/test partition (which entries were held out)
testbed/            minimal theory fixture for testing without AFP heaps
results/            raw model outputs from every evaluation run
docs/               project proposal and critical review
```

The AFP corpus, extracted data and model checkpoints are excluded for size and
are reproducible from the above.
