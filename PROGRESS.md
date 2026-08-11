# Isabelle Proof-State Harvester — Progress & Handover

**Status:** working end to end on real AFP entries. Not yet run at scale.
**Last verified:** `Depth-First-Search` (AFP 2026-06-29) → 161 probes, 109 states,
86 transitions, 100% alignment, 0 errors, 0 orphaned rows.
**Output format:** JSONL, `prefix` / `state` / `continuation` per row.

---

## 1. What we are building and why

The goal is a **training dataset for a proof-generating model**. Each row is:

```
given this proof state  →  a human wrote this tactic  →  producing this new state
```

To get that, you need the proof state at every step of every proof. But `.thy`
files on disk contain **only the tactics** — the goal states are not written
down anywhere. They exist only inside Isabelle while it processes the file.

So the only way to obtain them is to *run* Isabelle and ask it, one position at
a time. That is what this project does: it drives Isabelle the way a human
editor would, moving a cursor through the file and recording what the proof
state panel displays at each stop.

### The mental model

Think of the Isabelle IDE (jEdit, VSCode, Emacs). When you click somewhere in a
proof, a side panel shows the current goal. Move the cursor down one line, the
panel updates. We are automating exactly that: **move cursor → read panel →
record → repeat**, thousands of times, with no GUI.

```
      the .thy file                    what Isabelle's panel shows
      -----------------------------    ----------------------------------
      lemma rev_rev: "rev (rev xs)…"   goal (1 subgoal):
                                        1. rev (rev xs) = xs
        apply (induct xs)              goal (2 subgoals):
                                        1. rev (rev []) = []
                                        2. ⋀a xs. …
        apply simp                     goal (1 subgoal):
                                        1. ⋀a xs. …
        apply simp                     No subgoals!
      done                             (no output)
```

A **dataset row** pairs one panel reading with the *next* command:

```
state_before = "goal (1 subgoal): 1. rev (rev xs) = xs"
tactic       = "apply (induct xs)"
state_after  = "goal (2 subgoals): 1. …  2. …"
```

### How we talk to Isabelle: LSP

**LSP (Language Server Protocol)** is the standard way editors talk to language
tools. The editor and the tool are separate programs that exchange JSON
messages over stdin/stdout. VSCode uses it for Python, Rust, everything.

Isabelle ships an LSP server (`isabelle vscode_server`). We use the
`isabelle-emacs` fork, which adds PIDE extensions the upstream version lacks.

Our Python script pretends to be an editor:
- it sends `PIDE/caret_update` — "the cursor is now at line 11, column 19"
- Isabelle sends back `PIDE/state_output` — "here is the goal at that position"

The Python library `isabelle_lsp_client` handles the JSON plumbing; we supply
the logic for where to put the cursor and what to do with the answers.

---

## 2. Current status

### Verified working

| Stage | What it proves | Result |
|---|---|---|
| 1. `--self-test` | Parsing/logic correct, no Isabelle needed | 29 assertions pass |
| 2. `--mock` | Full pipeline with a fake Isabelle | 23 states, 19 transitions |
| 3. `--probe-api` | Library API matches our assumptions | all names confirmed |
| 4. Live sanity run | LSP wiring works, `-l HOL`, no AFP build | 17 states, 13 transitions |
| 5. Live AFP entry | Real proofs, ROOT parsing, imports | 109 states, 86 transitions |

### Not done yet

- **Scale.** ~100 s for one small theory; ~999 AFP entries. Naive = days.
- **Process reuse.** Isabelle restarts per file (30–60 s each). Biggest win available.
- **Session heaps.** 313 entries need only `HOL` (already built). The rest need
  `isabelle build` first.
- **Filtering.** The dataset is raw; `sorry`/`oops` proofs and error states are
  not yet excluded.

---

## 3. Files in the project

```
llm-finetune-project/
├── afp_harvest.py        ← main script: everything
├── panel_text.py         ← HTML → plain text (imported by the above)
├── validate_dataset.py   ← quality checks on the output
├── probe_state.py        ← standalone LSP diagnostic (kept for debugging)
├── testbed/thys/Sanity/  ← tiny test theory, imports only Main
│   ├── Sanity.thy
│   └── ROOT
└── afp-2026-06-29/thys/  ← the archive, 999 entries
```

`panel_text.py` **must** sit next to `afp_harvest.py` — it is imported, not
optional.

---

## 4. How `afp_harvest.py` works

Seven sections, in file order.

### 4.1 Symbol decoding

Isabelle writes `\<And>` for `⋀`. This section can load
`$ISABELLE_HOME/etc/symbols` to translate. **In practice it is a no-op** — the
HTML output path we use already delivers real Unicode. Harmless; left in place.

### 4.2 Scanning the theory file (`scan`, `probes_by_command`)

Before we can move the cursor we must decide *where* to move it.

Your original spec was: split on whitespace, put the cursor after every token.
That works but is wasteful — a 3,217-token file at ~1 s per probe is ~50
minutes, and most tokens are mid-command where the goal doesn't change.

So there are two modes:

- `--mode commands` (**default**) — one cursor stop at the **end of each Isar
  command**. On the test file: 26 stops instead of ~90.
- `--mode tokens` — your original spec, exhaustive, for comparison.

Finding command boundaries needs a small lexer, because this is a trap:

```isabelle
(* this comment mentions apply and qed *)
text ‹markup that says apply again›
```

Naive keyword search would find three phantom `apply` commands. `scan()`
therefore tracks comments `(* *)`, strings `"…"` and cartouches `‹…›`, and
never looks for keywords inside them. This is checked by the self-test.

A command span runs from a keyword to just before the next keyword; the cursor
goes at the end, with trailing comments trimmed.

**Isabelle does not require a space before a delimiter.** `proof(cases xs)`,
`by(auto)` and `apply(induct xs)` are all legal, and all lex as one
whitespace-delimited token. Matching tokens exactly against the keyword list
missed them, which cost real proof states (DFS.thy went from 151 to 161 probes
once fixed) *and* corrupted proof-block nesting — a missed `proof` left the
depth at 0, so a later `by` closed the wrong block. `command_keyword()` now
matches a leading keyword followed by `(`, `[`, `{` or `<`.

### 4.3 Positions (`LineIndex`)

LSP wants `(line, character)`, both **0-indexed**, with `character` counted in
UTF-16 code units. `LineIndex` converts a character offset in the file into
that pair. `--offset-encoding codepoint` switches counting if ever needed.

The dataset stores `line` **1-indexed** for human readability. That +1 is
applied once, where the row is built.

### 4.4 Sessions and ROOT files

AFP is laid out as `thys/<Session>/<Theory>.thy`, and each entry has a `ROOT`
file:

```
session Depth-First-Search (AFP) = HOL +
```

The name after `=` is the **parent**: the pre-built heap Isabelle must load
before it can process this entry. `parse_roots()` extracts it by regex so each
theory is started with the right `-l` argument. `--logic` overrides it.

### 4.5 Driving Isabelle (`Harvester`)

The part that took the most discovery. Key facts about `isabelle_lsp_client`
0.0.2, all verified against the installed source rather than assumed:

- **`process.run(args)` never returns.** It starts Isabelle and loops forever.
  It builds the real client *internally*; you cannot reach it from outside.
- Therefore **all work happens inside callbacks.** The library calls
  `on_start(document)` once the theory is open and processed. Our entire
  cursor sweep runs inside that callback.
- **Callbacks must be `async` and take `(document, response, timestamp)`.**
- `document.move_caret(line, character)` moves the cursor.

Lifecycle:

```
harvest_file()
   ├─ builds the list of cursor positions
   ├─ registers callbacks
   ├─ starts process.run() as a background task
   └─ waits for either: sweep finished, or Isabelle died

        on_start(document)              ← library calls this when ready
           ├─ send PIDE/state_init      ← REQUIRED, see below
           └─ for each position:
                 move_caret(...)
                 wait for state_output
                 record if changed
```

#### The two panels — the single most important discovery

Isabelle has **two** output channels, and they are easy to confuse:

| | `PIDE/dynamic_output` | `PIDE/state_output` |
|---|---|---|
| Panel | Output panel | **State panel** |
| Contains | hints, warnings, finished theorem | **the proof state** |
| At an `apply` step | **empty** | the goal |

The original plan used `dynamic_output`. It is the wrong channel — it is empty
at exactly the `apply` steps you need. We now use `state_output`, which
requires sending a `PIDE/state_init` **request** first. Without that init, the
state panel never exists and no messages are ever sent.

Once initialised the panel has `auto_update = true` and refreshes on every
cursor move, so no extra polling is needed.

#### The HTML workaround

Isabelle can emit panel content as plain text or HTML. **The plain-text path is
broken in `Isabelle2025-2-vsce`** — it crashes server-side:

```
*** Session consumer failure: "isabelle.vscode.Dynamic_Output"
*** Bad JSON value: isabelle.vscode.LSP$$$Lambda/0x…
```

In `pretty_text_panel.scala`, the plain-text branch attaches a decoration
object that Isabelle's own JSON encoder cannot serialise. The HTML branch does
not. Both panels share this code, so both are affected.

The library hardcodes `-o vscode_html_output=false`, which triggers the bug.
**The script now appends `-o vscode_html_output=true` automatically** (a later
`-o` wins), and `panel_text.py` converts the HTML back to plain text.

This is lossless: Isabelle's pretty-printer has already done the line wrapping
before rendering to HTML, so the text content of the HTML is character-for-
character the plain-text state.

#### The timing logic

After each cursor move, how long do you wait?

- Waiting a fixed 2 s is safe but slow.
- Taking the first message is fast but **wrong**: Isabelle re-prints the goal
  several times while a command is still elaborating. The Emacs client has the
  same problem and deliberately delays 0.3 s to discard stale renders.

So: wait up to `--settle` (default 2.0 s) for a first message, then return as
soon as output has been quiet for `--quiet` (default 0.35 s). Observed: ~0.6 s
for a successful probe.

**A miss is normal, not an error.** The panel only emits when content
*changes*, so `done`, `qed`, `text` and `end` legitimately produce nothing.
Those cost the full `--settle`, which is why lowering it speeds things up.

### 4.6 Building transitions

States are recorded first, then paired: row *i−1*'s state is what row *i*'s
command acted on.

One rule matters a great deal. Commands that **open a new proof** (`lemma`,
`theorem`, `fun`, `datatype`, …) are excluded as tactics. Without that
exclusion you get rows like:

```
BEFORE: No subgoals!                       ← end of one proof
TACTIC: lemma append_assoc: "…"            ← start of a different proof
```

which would teach the model to emit `lemma` whenever a proof finishes. This is
silent garbage — the row looks structurally fine. Guarded by a self-test.

### 4.7 Proof blocks and the training triple

Each row needs three things: what the model has already seen (`prefix`), the
goal it must act on (`state`), and what actually came next (`continuation`).

Because the caret sits at the **end** of a command:

```
prefix        ends with the command just executed
state         the goal that command produced
continuation  begins with the command to predict
```

`continuation` is scoped to the **enclosing proof**, not the file. That keeps
rows small and makes the end of a proof meaningful. `proof_blocks()` finds
those spans, tracking nesting so a `by` closing an inner `have` is not mistaken
for the end of the outer proof.

Critically, `proof_blocks()` and `probes_by_command()` both call
`command_spans()`. They must agree exactly: when they were computed
independently, a block closed by `by (auto simp: foo)` ended after `by`, the
caret at the end of the full command fell outside every block, and 21 rows
silently lost their prefix and continuation. Sharing the span builder makes
that class of bug impossible.

### 4.8 The stop signal — not what you would expect

The intuition is that the last row of a proof has an empty `continuation`.
**In harvested data this never happens.**

Isabelle emits no state at the position that closes a proof. `done` produces no
message; so does a `by` that closes a whole lemma. The state panel only fires
when its content *changes*, and after the proof is finished there is nothing
new to show. So the closing position never becomes a row.

The signal is one step earlier. The final row of `rev_rev` is:

```
state:        proof (prove)  goal: No subgoals!
continuation: done
```

The model learns *No subgoals → emit `done` → stop*. Each row therefore carries
`remaining`, the number of commands left in the proof after the caret:

- `remaining == 1` — last row you will actually get; only the closer remains
- `remaining == 0` — the closing position itself; expect ~0 of these

On DFS.thy: 23 proof blocks, 22 rows at `remaining == 1`, 0 at `remaining == 0`.

### 4.9 Output

JSONL — one record per line, newlines inside states escaped, streams straight
into training pipelines.

```
out_dir/states.jsonl              the training rows
out_dir/transitions.jsonl         state_before / tactic / state_after
out_dir/states.checkpoint.jsonl   crash log, used by --resume
```

`--format json` or `both` if a JSON array is also wanted. The checkpoint stays
JSONL regardless, because a JSON array cannot be appended to safely mid-run;
`--resume` reads it and skips finished files, and `transitions` is always
rebuilt from the whole checkpoint so a resumed run is still complete.

**`states.jsonl` is the training file.** Abridged record:

```json
{"line": 11, "command": "apply", "in_proof": true, "remaining": 3, "offset": 372,
 "prefix": "lemma rev_rev [simp]: \"rev (rev xs) = xs\"\n  apply (induct xs)",
 "state": "proof (prove)\ngoal (2 subgoals):\n 1. rev (rev []) = []\n 2. …",
 "continuation": "\n   apply simp\n  apply simp\n  done"}
```

**Filter on `in_proof` before training.** Rows outside any proof (`theory`,
`imports`, `begin`, `end`) also have an empty continuation, but that is not a
proof-finished signal. In practice most of those produce no state at all, so
they rarely appear — but the flag is there to be safe.

`--prefix-scope file` widens `prefix` to the whole theory. Much larger: proof
scope gave a median of 204 characters on DFS.thy, max 786.

## 5. Running it

### Offline checks (no Isabelle)

```bash
python afp_harvest.py --self-test
python afp_harvest.py --afp testbed/thys --mock --out out_mock --log-dir out_logs
python afp_harvest.py --afp testbed/thys --dry-run     # show cursor positions only
```

### Sanity run (needs Isabelle, no AFP build)

```bash
python afp_harvest.py \
  --afp testbed/thys \
  --isabelle /path/to/isabelle-emacs/bin/isabelle \
  --logic HOL \
  --out out_sanity --log-dir out_logs \
  --trace --dump-raw
```

Expect 17 states, 13 transitions.

### Real AFP entry

```bash
python afp_harvest.py \
  --afp afp-2026-06-29/thys \
  --include Depth-First-Search \
  --isabelle /path/to/isabelle-emacs/bin/isabelle \
  --session-dirs afp-2026-06-29/thys \
  --out out_afp --log-dir out_logs -v
```

`--session-dirs` puts the AFP on Isabelle's search path so imports resolve.
Omit `--logic` so the parent is read from ROOT.

### Validate

```bash
python validate_dataset.py out_afp --sample 3
```

### Useful flags

| Flag | Purpose |
|---|---|
| `--include REGEX` | only files whose path matches |
| `--limit N` | only the first N files |
| `--resume` | skip files already in the checkpoint |
| `--trace` | print every cursor stop and its state |
| `--dump-raw` | log every message with timings to `logs/` |
| `--settle` / `--quiet` | timing knobs |
| `-o NAME=VALUE` | extra Isabelle option (repeatable) |
| `--prefix-scope proof\|file` | context window for `prefix` (default proof) |
| `--format jsonl\|json\|both` | output encoding (default jsonl) |
| `--show-blocks` | print proof-block segmentation offline, then exit |
| `-v` | one log line per state |

---

## 6. What the validator checks

Run it after every harvest. The critical check is **alignment**: it re-reads
each source file and confirms the text ending at the recorded `(line, column)`
really is the command in `probe`.

This matters because if the cursor were landing one position off, every state
would be attributed to the **wrong command** and the dataset would still look
perfectly plausible. Nothing else would catch it. Both live runs reported
100%.

The second structural check is **`proof commands marked outside a proof`**. If
a row's command is `apply`/`by`/`show`/`qed` but it is not inside any proof
block, the block detector and the command scanner disagree — and those rows
lose their prefix and continuation silently. This must always be 0.

It also reports empty states, states containing Isabelle error text, leftover
un-stripped HTML, files with suspiciously few states (usually a failed load),
the command distribution, prefix length percentiles, the `remaining`
distribution, and which files contain `sorry`/`oops`.

**Read the aggregates, not just the errors.** Both structural bugs found so far
surfaced as implausible counts (`proof-finished rows: 0` in a file full of
finished proofs), never as an exception.

---

## 7. Known issues and next steps

### Ordered by payoff

**1. One Isabelle process per file.** Currently we restart for every theory,
costing 30–60 s each. `write_loop` accepts an `args["theories"]` list and opens
each as a Document before the main one — the library already anticipates reuse.
Sharing one process across an entry's theories is the single biggest speedup
and the main outstanding change to `harvest_file()`.

**2. Lower `--settle`.** Successful probes take ~0.6 s; misses cost the full
2.0 s and misses are common. Test `--settle 1.0` against the same entry — if
the state count is unchanged, the shorter wait costs nothing.

**3. Session heaps.** Parent distribution across the 999 entries:

```
313  HOL                 ← already built, no work needed
 97  HOL-Library
 63  HOL-Analysis
 59  HOL-Probability
 22  HOL-Number_Theory
```

Start with the 313. Then `isabelle build -b HOL-Library HOL-Analysis` unlocks
~160 more. That reaches ~470 entries before anything exotic is required.

**4. Filtering before training.** The output is raw. Drop rows with
`in_proof: false`, rows whose `state`/`output` contains error text, and rows
from files containing `sorry`/`oops`. Consider splitting off `by`/`done`/`qed`
rows — they are the easiest steps and will otherwise dominate (19% of DFS
transitions).

**5. Isar command coverage.** `probes_by_command()` uses a keyword heuristic. A
bound variable literally named `show` or `case` would be misread. The
`isabelle_parser` Lark grammar would give exact command spans; it is a drop-in
replacement for that one function, and nothing downstream changes.

### Notes for the HPC move

- **Everything is per-file independent.** Different entries can run as separate
  jobs with different `--out` directories, then concatenate the JSON at the
  end. This parallelises trivially and is the natural fit for a job array.
- **`--resume` makes jobs restartable** after a walltime kill.
- Each Isabelle process wants **several GB of RAM** and uses multiple cores;
  size the job accordingly rather than packing many per node.
- `isabelle build` heaps are large. Build them **once** to a shared location
  and point every job at it.
- Isabelle needs a writable `$ISABELLE_HOME_USER`. On a cluster, set it
  explicitly to scratch rather than letting it default into a home quota.
- Network is not needed at harvest time — only for the initial AFP download and
  heap builds.

---

## 8. Debugging guide

**Zero states from a file.** Almost always the session heap. Check
`logs/<Theory>.isabelle.log` and confirm the parent from ROOT is built.

**`Bad JSON value: …Lambda`.** `vscode_html_output` got set to false. The
script appends `true` automatically; only appears if overridden.

**Everything reports `<no output>`.** The state panel was never initialised —
check that `state panel initialised (state_id=…)` appears in the log.

**Alignment below 95%.** Try `--offset-encoding codepoint`, then verify the
client is 0-indexed.

**Tuning timing.** `--dump-raw` writes every message with timestamps:

```bash
python -c "
import json
for l in open('out_logs/DFS.dynamic_output.jsonl'):
    d = json.loads(l)
    print(f\"{d['t']:7.2f} {d['kind']:7} {'CHG' if d['changed'] else '   '} \"
          f\"{d['content'][:60]}\")"
```

Clusters within ~0.3 s mean Isabelle is still elaborating — `--quiet` must
outlast the gap inside a cluster.

**Proof blocks look wrong.** `--show-blocks` prints the segmentation offline,
with no Isabelle:

```bash
python afp_harvest.py --afp afp-2026-06-29/thys \
  --include Depth-First-Search --show-blocks
```

Each block shows its line range, opening and closing text, and whether a probe
lands on its end (`END-PROBED`). Any `END NOT PROBED <<<` means the block
detector and the command scanner have drifted apart.

**`probe_state.py`** is a standalone minimal reproduction of the state-panel
handshake. If the main script misbehaves, run it to isolate LSP problems from
harvester problems.
