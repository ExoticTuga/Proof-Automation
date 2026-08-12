# AFP proof-state harvester

Extracts `(proof state → tactic → new proof state)` training rows from Isabelle
theory files by driving `isabelle-emacs` over LSP.

**Status: working end to end, not yet run at scale.** This is step one of a
larger fine-tuning project. See `PROGRESS.md` for how it works, why the design
is what it is, and what remains.

## Layout

```
afp_harvest.py        main script
panel_text.py         HTML → text (imported by afp_harvest, must sit beside it)
validate_dataset.py   quality checks — run after every harvest
probe_state.py        standalone LSP diagnostic, for debugging only
testbed/thys/Sanity/  tiny test theory (Sanity.thy + ROOT), imports only Main
afp-2026-06-29/thys/  the archive
PROGRESS.md           full write-up
```

Input directories must be laid out as `<afp>/<Session>/<Theory>.thy` — pass the
level *above* the session folders (`--afp afp-2026-06-29/thys`).

## Quick start

Offline, no Isabelle needed:

```bash
python afp_harvest.py --self-test
python afp_harvest.py --afp testbed/thys --mock --out out_mock --log-dir out_logs
```

Live sanity check (needs Isabelle, no AFP build — `Sanity.thy` imports only
`Main`, which separates "LSP wiring works" from "heaps are built"):

```bash
python afp_harvest.py \
  --afp testbed/thys \
  --isabelle /path/to/isabelle-emacs/bin/isabelle \
  --logic HOL \
  --out out_sanity --log-dir out_logs --trace
```

Expect 17 states, 13 transitions.

Real AFP entry:

```bash
python afp_harvest.py \
  --afp afp-2026-06-29/thys \
  --include Depth-First-Search \
  --isabelle /path/to/isabelle-emacs/bin/isabelle \
  --session-dirs afp-2026-06-29/thys \
  --out out_afp --log-dir out_logs -v

python validate_dataset.py out_afp --sample 3
```

Expect 99 states, 77 transitions, 100% alignment.

Omit `--logic` on AFP entries so the parent session is read from `ROOT`.
`--session-dirs` puts the archive on Isabelle's search path so imports resolve.

## Output

JSONL (one record per line — newlines inside proof states are escaped, and the
format streams directly into training pipelines).

```
out_dir/states.jsonl              the training rows: prefix / state / continuation
out_dir/transitions.jsonl         state_before / tactic / state_after
out_dir/states.checkpoint.jsonl   crash log, used by --resume; delete to restart
```


**`states.jsonl` is the training file.** One record (abridged):

```json
{"line": 11, "command": "apply", "in_proof": true, "offset": 372,
 "prefix": "lemma rev_rev [simp]: \"rev (rev xs) = xs\"\n  apply (induct xs)",
 "state": "proof (prove)\ngoal (2 subgoals):\n 1. rev (rev []) = []\n 2. …",
 "continuation": "\n   apply simp\n  apply simp\n  done"}
```

The caret sits at the **end** of a command, so `prefix` ends with the command
just executed, `state` is the goal it produced, and `continuation` begins with
the command to predict.

`continuation` is scoped to the **enclosing proof**, so it is empty exactly
when the proof is finished — that is the stop signal. `--prefix-scope file`
widens `prefix` to the whole theory (much larger files).

**`in_proof` must be filtered on.** Rows outside any proof (`theory`,
`imports`, `begin`, `end`) also have an empty continuation, but that is not a
proof-finished signal. Drop `in_proof: false` before training.

## Flags

| Flag | Purpose |
|---|---|
| `--include REGEX` | only paths matching |
| `--limit N` | only the first N files |
| `--resume` | skip files already in the checkpoint |
| `--mode commands\|tokens` | cursor per Isar command (default) or per whitespace token |
| `--prefix-scope proof\|file` | context window for `prefix` (default proof) |
| `--format jsonl\|json\|both` | output encoding (default jsonl) |
| `--settle` / `--quiet` | timing knobs (2.0 / 0.35 s) |
| `--trace` | print every cursor stop and the state it returned |
| `--dump-raw` | log every LSP message with timings |
| `-o NAME=VALUE` | extra Isabelle option, repeatable |
| `-v` | one log line per state |

## Two things worth noting

**We read `PIDE/state_output`, not `PIDE/dynamic_output`.** They are different
panels. `dynamic_output` is the *output* panel — hints and the finished theorem
— and it is **empty at exactly the `apply` steps you need**. The state panel
also has to be switched on with a `PIDE/state_init` request before it sends
anything.

**`-o vscode_html_output=true` is mandatory** and is appended automatically.
The plain-text path crashes server-side in `Isabelle2025-2-vsce`
(`Bad JSON value: …Lambda`); `panel_text.py` converts the HTML back, losslessly.

## Not done yet

- One Isabelle process per file (30–60 s startup each) — biggest speedup available
- Session heaps: 313 of 999 entries need only `HOL`; start there
- Filtering `sorry`/`oops` proofs and error states out of the training rows

Details and priorities in `PROGRESS.md` §7.
