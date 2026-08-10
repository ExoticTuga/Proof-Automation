# AFP proof-state harvester

Drives `isabelle-emacs` over LSP, walks a caret through every Isar command in a
`.thy` file, and records the `PIDE/dynamic_output` proof state at each stop.

Output is JSON arrays in `--out`:

- `states.json` — one object per distinct proof state
- `transitions.json` — the training rows: `state_before`, `tactic`, `state_after`
- `states.checkpoint.jsonl` — append-only crash log, only used by `--resume`

(A JSON array can't be appended to safely mid-run, so the checkpoint stays JSONL.
Delete it and the next run starts clean.)

## Test it in five stages

Do these in order. Each one isolates a different failure, so when something
breaks you know which layer it's in.

### 1. Offline logic — no Isabelle, no venv needed

```bash
python afp_harvest.py --self-test
```

17 assertions over the lexer, caret positions, symbol decoding, transition
pairing, and ROOT parsing. Notably it checks that `apply` appearing inside a
comment or a cartouche is *not* picked up as a command, which is the failure
that would quietly poison your dataset. All should PASS.

### 2. Full pipeline with a fake Isabelle

```bash
python afp_harvest.py --afp testbed/thys --mock --out /tmp/mock
python validate_dataset.py /tmp/mock --sample 2
```

`--mock` swaps in a backend that returns synthetic goals without launching
anything. This exercises discovery, probing, dedup, transition building, and
JSON writing. If this works and the real run doesn't, the problem is the LSP
layer specifically.

### 3. Does the client API match?

```bash
source .venv/bin/activate
python afp_harvest.py --probe-api
```

Prints the actual classes and methods of the installed `isabelle_lsp_client`.
Check that `IsabelleProcess`, `IsabelleClient`, `ClientHandler`,
`register_on_dynamic_output`, and `caret_update` all exist with those names. If
any differ, `IsabelleSession` (one class, ~120 lines) is the only thing to edit.

### 4. First live run — one tiny theory, `-l HOL`

```bash
python afp_harvest.py \
  --afp testbed/thys \
  --isabelle /path/to/isabelle-emacs/bin/isabelle \
  --isabelle-home /path/to/isabelle-emacs \
  --logic HOL \
  --out /tmp/sanity --trace --dump-raw
```

`testbed/thys/Sanity/Sanity.thy` imports only `Main`, so **no AFP build is
required** — this deliberately separates "my LSP wiring works" from "my session
heaps are built." `--trace` prints every caret probe and the state it returned,
so you can watch it in real time.

What correct output looks like:

```
=== L11:19 [apply] apply (induct xs)  (0.41s)
    proof (prove)
    goal (2 subgoals):
     1. rev (rev []) = []
     2. ...
```

The goal count should go 1 → 2 → 1 → *No subgoals* across each induction. If
the states are all identical, or lag one command behind, see "Reading the raw
dump" below.

Then:

```bash
python validate_dataset.py /tmp/sanity --sample 3
```

Expect ~14 states, 100% alignment, 0 errors.

### 5. One real AFP entry

```bash
isabelle build -d afp/thys -b -v Abel_Limit_Theorem   # builds parent heaps too

python afp_harvest.py \
  --afp afp/thys --include Abel_Limit \
  --isabelle /path/to/isabelle-emacs/bin/isabelle \
  --isabelle-home /path/to/isabelle-emacs \
  --session-dirs afp/thys \
  --out /tmp/abel --dump-raw -v

python validate_dataset.py /tmp/abel --sample 3
```

Only after this is clean should you drop `--include` and run the whole archive
with `--resume`.

## What the validator is actually checking

The important one is **alignment**. It re-reads each source file and confirms
that the text ending at the recorded `(line, character)` really is the command
in `probe`. If your client turns out to be 1-indexed, or counts characters
differently, every state gets attributed to the wrong command — and the dataset
still *looks* fine. Nothing else catches that.

It also reports empty states, states containing Isabelle error text, files with
suspiciously few states (usually a failed load), the command distribution, and
which files contain `sorry`/`oops` so you can exclude them.

Alignment below 95% → try `--offset-encoding codepoint`, then check whether
`caret_update` is 0-indexed in your client version.

## Reading the raw dump

`--dump-raw` writes `logs/<Theory>.dynamic_output.jsonl` — every single
`PIDE/dynamic_output` message with a timestamp and whether it differed from the
previous one:

```bash
python -c "
import json,sys
for l in open('logs/Sanity.dynamic_output.jsonl'):
    d=json.loads(l)
    print(f\"{d['t']:7.2f} {'CHG' if d['changed'] else '   '} {d['content'].splitlines()[0][:60]}\")"
```

Use it to tune the two timing knobs:

- clusters of several messages within ~0.3 s of each other → Isabelle is
  re-printing while elaborating; your `--quiet` window must be longer than the
  gap inside a cluster
- long gaps before the *first* message after a caret move → raise `--settle`
- if the last message in a cluster is the good one but you're capturing an
  earlier one, `--quiet` is too short

## Things that will bite you

**AFP theories will not load under `-l HOL`.** Nearly every entry imports
`HOL-Library`, `HOL-Analysis`, or another AFP session. Build heaps first
(`isabelle build -d afp/thys -b <Session>`), then start the server with the
session's *parent* as the logic so the theory itself is still elaborated live.
The script reads `ROOT` to find the parent; override with `--logic`. Zero states
from a file is almost always this — check `logs/<Theory>.isabelle.log`.

**Two seconds per token is a trap.** Your test file has 3,217 tokens; a flat 2 s
wait is 107 minutes for one theory. `--mode commands` (default) probes once per
Isar command instead — 26 probes instead of ~90 on the sanity file. And
`--settle 2.0 --quiet 0.35` waits *up to* 2 s but returns as soon as output has
been stable for 0.35 s. `--mode tokens` reproduces the original spec exactly if
you want to compare.

**Don't take the first `dynamic_output` after a caret move.** Isabelle
re-prints the goal several times while a command is still elaborating — the
lsp-isar Emacs client cancels and re-renders on a 0.3 s delay for exactly this
reason. Message #1 is often stale or half-formed.

**Decorations carry no proof content.** `PIDE/dynamic_output` sends
`params.content` (the state) *and* `params.decorations` (highlight ranges).
Only `content` is read.

**Startup dominates.** One Isabelle process per file costs 30–120 s. The current
code restarts per file for isolation, which is right while debugging but the
first thing to change for a full-archive run — keep one process alive per
session and swap theories.

## Where `isabelle_parser` fits

It replaces the heuristic in `probes_by_command()`. The current scanner treats
any bare word matching an Isar keyword as a command start; it's comment-,
string- and cartouche-aware but not grammar-aware, so a bound variable literally
named `show` or `case` would be misread. A Lark parse tree gives exact command
spans plus the command's structural type. Nothing downstream changes —
`probes_by_command` just has to keep returning
`Probe(offset, line, character, probe_text, command)`.

## Filtering before you train

`transitions.json` is raw. Drop rows where `state_after` contains error text,
where the enclosing lemma was closed with `sorry`/`oops`, and where
`state_before` is empty. Rows with a `command` of `by`/`done`/`qed` are the
proof-closing steps — worth a separate split, since they're the easiest and will
otherwise dominate. The validator prints all of these counts.
