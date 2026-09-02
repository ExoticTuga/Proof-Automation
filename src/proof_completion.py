#!/usr/bin/env python3
"""
proof_completion.py -- can the model prove a held-out theorem?

    python proof_completion.py \
        --model runs/qwen7b/final \
        --afp src/afp-2026-06-29/thys \
        --entries split_hol/test_entries.txt \
        --isabelle ~/isabelle-emacs/bin/isabelle \
        --out results/completion.jsonl --limit 100

WHAT THIS MEASURES, AND WHY IT DIFFERS FROM EXACT MATCH
------------------------------------------------------
Exact match asks whether the model reproduced the command a particular human
wrote. Proof completion asks whether Isabelle ACCEPTS what the model wrote.
The two come apart constantly: `by auto`, `by simp` and `by blast` often close
the same goal, so exact match penalises a correct proof for being a different
correct proof. Completion credits any proof the kernel accepts.

METHOD
------
For each held-out theorem the proof body is deleted and regenerated from
scratch, one command at a time, with Isabelle in the loop:

    1. place the caret after the theorem statement, read the goal
    2. ask the model for the next command
    3. append it, re-elaborate, read the new goal
    4. repeat until no subgoals remain, an error appears, or a step budget
       is exhausted

This is closed-loop: the model sees the *actual* state its own previous
command produced, not the state the human's proof would have produced. A
single wrong step therefore changes every subsequent state, which is exactly
the difficulty a real proof assistant user faces and which per-step metrics
conceal.

A theorem counts as PROVED when the goal is discharged with no error
diagnostic over the generated region. `sorry` and `oops` are rejected
explicitly: they are accepted by Isabelle but prove nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from afp_harvest import (  # noqa: E402
    LineIndex, command_spans, proof_blocks, scan,
)
from panel_text import panel_html_to_text  # noqa: E402

try:
    from afp_harvest import load_keyword_declarations
except ImportError:
    load_keyword_declarations = None

log = logging.getLogger("completion")

STATE_INIT = "PIDE/state_init"
STATE_OUTPUT = "PIDE/state_output"

# Commands that end a proof without proving anything.
CHEATS = ("sorry", "oops")

ERROR_MARKERS = (
    "Failed to", "Undefined", "Type unification failed", "Inner syntax error",
    "Outer syntax error", "Malformed", "Illegal application", "*** ",
    "Timeout", "No such", "Bad ", "Unknown ", "Ill-typed",
)

PROMPT_TEMPLATE = (
    "(* Isabelle/HOL proof. Given the theory context and the current proof "
    "state, give the next Isar command. *)\n\n"
    "### Context\n{prefix}\n\n"
    "### Proof state\n{state}\n\n"
    "### Next command\n"
)


# --------------------------------------------------------------------------- #
# Targets: theorems to prove
# --------------------------------------------------------------------------- #

@dataclass
class Target:
    file: str
    entry: str
    line: int                 # 1-indexed line of the opening command
    stmt_end: int             # char offset just after the statement
    body_end: int             # char offset at the end of the original proof
    statement: str
    reference: str            # the human's proof, for comparison


def load_harvested(states_path: Path) -> dict[str, set[int]]:
    """(file -> offsets at which extraction recorded a proof state).

    Only theorems whose statement position ALREADY yielded a state during
    extraction are attempted. Without this filter the sampler picks theorems
    deep inside large theories in heavy sessions, where Isabelle has not
    finished elaborating the imports -- let alone two thousand preceding
    lines -- by the time the caret arrives, and every attempt fails with an
    empty state for reasons that have nothing to do with the model.
    """
    by_file: dict[str, set[int]] = {}
    if not states_path.is_file():
        return by_file
    with states_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("in_proof") and r.get("state", "").strip():
                by_file.setdefault(r["file"], set()).add(r.get("offset", -1))
    return by_file


def find_targets(thy: Path, min_steps: int, max_steps: int,
                 elaborated: Optional[set[int]] = None) -> list[Target]:
    """Theorems in a file whose proof is a reasonable length to attempt."""
    text = thy.read_text(encoding="utf-8", errors="replace")
    spans = command_spans(text)
    out: list[Target] = []

    for s, e in proof_blocks(text):
        inner = [(kw, cs, ce) for kw, cs, ce in spans if s <= cs < e]
        if not inner:
            continue
        opener_kw, _, stmt_end = inner[0]
        if opener_kw not in ("lemma", "theorem", "corollary", "proposition"):
            continue
        n_steps = len(inner) - 1
        if not (min_steps <= n_steps <= max_steps):
            continue
        if any(w in text[s:e] for w in CHEATS):
            continue
        if elaborated is not None and stmt_end not in elaborated:
            continue          # extraction never got a state here
        out.append(Target(
            file=str(thy), entry=thy.parent.name,
            line=text[:s].count("\n") + 1,
            stmt_end=stmt_end, body_end=e,
            statement=" ".join(text[s:stmt_end].split()),
            reference=" ".join(text[stmt_end:e].split()),
        ))
    return out


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

class Tactician:
    """Wraps the fine-tuned model; returns candidate next commands."""

    def __init__(self, model_path: str, max_new_tokens: int = 48) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(model_path,
                                                 trust_remote_code=True)
        if self.tok.pad_token_id is None:
            self.tok.pad_token = self.tok.eos_token
        self.tok.padding_side = "left"
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path, dtype=torch.bfloat16, device_map="auto",
            trust_remote_code=True)
        self.model.eval()
        self.max_new_tokens = max_new_tokens

    def propose(self, prefix: str, state: str, k: int,
                temperature: float) -> list[str]:
        prompt = PROMPT_TEMPLATE.format(prefix=prefix[-6000:],
                                        state=state[:4000])
        enc = self.tok(prompt, return_tensors="pt", truncation=True,
                       max_length=1984, add_special_tokens=False
                       ).to(self.model.device)
        kw = dict(max_new_tokens=self.max_new_tokens,
                  pad_token_id=self.tok.pad_token_id,
                  num_return_sequences=k)
        if k > 1 or temperature > 0:
            kw.update(do_sample=True, temperature=max(temperature, 1e-3),
                      top_p=0.95)
        else:
            kw.update(do_sample=False)
        with self.torch.no_grad():
            out = self.model.generate(**enc, **kw)
        new = out[:, enc["input_ids"].shape[1]:]
        cands, seen = [], set()
        for t in self.tok.batch_decode(new, skip_special_tokens=True):
            c = " ".join(t.split("\n")[0].split())
            if c and c not in seen:
                seen.add(c)
                cands.append(c)
        return cands


# --------------------------------------------------------------------------- #
# Isabelle session held open across many attempts
# --------------------------------------------------------------------------- #

class IsabelleSession:
    """One Isabelle process, driven over LSP, reused for every attempt.

    Startup costs 30-60 s, so a fresh process per theorem would dominate the
    runtime. The document is edited in place instead and Isabelle
    re-elaborates incrementally.
    """

    def __init__(self, exec_path: str, options: list[str], log_path: Path):
        self.exec_path = exec_path
        self.options = options
        self.log_path = log_path
        self.document = None
        self.ready = asyncio.Event()
        self._state: Optional[str] = None
        self._changed = asyncio.Event()
        self._diagnostics: list = []
        self._runner = None

    # -- callbacks ---------------------------------------------------------- #

    async def on_state_output(self, document, response, timestamp) -> None:
        content = (response.get("params") or {}).get("content")
        if content is None:
            return
        text = panel_html_to_text(content)
        if text != self._state:
            self._state = text
            self._changed.set()

    async def on_diagnostics(self, document, response, timestamp) -> None:
        params = response.get("params") or {}
        if self.document and params.get("uri") == self.document.uri:
            self._diagnostics = params.get("diagnostics", []) or []

    async def on_start(self, document, **kw) -> None:
        self.document = document
        lsp = None
        for name in ("lspClient", "lsp_client", "client", "_lspClient"):
            c = getattr(document.isabelle, name, None)
            if c is not None and hasattr(c, "request"):
                lsp = c
                break
        if lsp is None:
            raise RuntimeError("no LSPClient on the document")
        from proof_completion import RawRequest
        await lsp.request(RawRequest(STATE_INIT), timeout=60)
        self.ready.set()

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self, theory: Path) -> None:
        from isabelle_lsp_client import ClientHandler, IsabelleProcess
        handler = ClientHandler()
        handler.register_on_start(self.on_start)
        handler.register(STATE_OUTPUT, self.on_state_output)
        handler.register_on_publish_diagnostics(self.on_diagnostics)
        args = {
            "exec": self.exec_path,
            "options": self.options,
            "log_path": str(self.log_path),
            "theory": str(theory.resolve()),
            "startup_timeout": 300,
        }
        process = IsabelleProcess(handler)
        self._runner = asyncio.create_task(process.run(args))
        done = asyncio.create_task(self.ready.wait())
        await asyncio.wait({self._runner, done},
                           return_when=asyncio.FIRST_COMPLETED)
        if self._runner.done() and not self.ready.is_set():
            exc = self._runner.exception()
            raise exc if exc else RuntimeError("Isabelle exited during startup")

    async def stop(self) -> None:
        if self._runner and not self._runner.done():
            self._runner.cancel()
            try:
                await self._runner
            except BaseException:
                pass

    # -- editing and probing ------------------------------------------------ #

    async def set_text(self, text: str) -> None:
        """Replace the whole document, then let Isabelle re-elaborate."""
        from lsp_client import ContentChange
        doc = self.document
        doc.lines = text.split("\n")
        change = ContentChange(text=text)
        await doc.apply_changes([change])

    async def state_at(self, line: int, character: int,
                       settle: float = 8.0, quiet: float = 0.5) -> str:
        """Move the caret and wait for the state panel to settle."""
        self._changed.clear()
        before = self._state
        await self.document.move_caret(line, character)
        deadline = time.monotonic() + settle
        got = False
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                await asyncio.wait_for(self._changed.wait(),
                                       timeout=min(left, quiet))
            except asyncio.TimeoutError:
                if got:
                    break
                continue
            self._changed.clear()
            got = True
        if not got and self._state == before:
            return self._state or ""
        return self._state or ""

    async def warm_up(self, text: str, first: "Target",
                      budget: float) -> bool:
        """Poll until the theory produces a state, or the budget runs out."""
        idx = LineIndex(text)
        line, char = idx.position(first.stmt_end)
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            s = await self.state_at(line, char, settle=10.0, quiet=1.0)
            if s.strip():
                return True
            await asyncio.sleep(2.0)
        return False

    def errors(self) -> list[str]:
        return [d.get("message", "") for d in self._diagnostics
                if d.get("severity", 1) == 1]


class RawRequest:
    """Minimal stand-in for lsp_client's BaseRequest."""

    def __init__(self, method: str, params=None) -> None:
        self.jsonrpc = "2.0"
        self.id = None
        self.method = method
        self.params = params

    def model_dump(self, exclude_none: bool = True) -> dict:
        d = {"jsonrpc": self.jsonrpc, "id": self.id,
             "method": self.method, "params": self.params}
        return {k: v for k, v in d.items() if not (exclude_none and v is None)}


# --------------------------------------------------------------------------- #
# One attempt
# --------------------------------------------------------------------------- #

def goal_is_closed(state: str) -> bool:
    s = state.lower()
    return "no subgoals" in s or (
        "goal" in s and "subgoal" not in s and "this:" not in s)


@dataclass
class Attempt:
    file: str
    entry: str
    line: int
    statement: str
    reference: str
    generated: list[str] = field(default_factory=list)
    proved: bool = False
    reason: str = ""
    steps: int = 0
    seconds: float = 0.0


async def attempt_proof(sess: IsabelleSession, tac: Tactician, original: str,
                        t: Target, cfg) -> Attempt:
    """Regenerate one theorem's proof with Isabelle in the loop.

    The document is TRUNCATED after the generated body: everything following
    the theorem in the original file is dropped. Retaining it produces
    `Bad context for command "lemma"` on the very first step, because a
    statement with no proof yet leaves the following command unreachable --
    an error about the tail, not about anything the model wrote. Truncating
    also shortens what Isabelle must re-elaborate on each step.
    """
    a = Attempt(file=t.file, entry=t.entry, line=t.line,
                statement=t.statement, reference=t.reference)
    t0 = time.monotonic()
    head = original[:t.stmt_end]
    body = ""
    closed_goal = False

    for step in range(cfg.max_steps + 1):
        doc_text = head + body
        await sess.set_text(doc_text)

        idx = LineIndex(doc_text)
        line, char = idx.position(len(doc_text))
        state = await sess.state_at(line, char, settle=cfg.settle)

        errs = [e for e in sess.errors() if any(m in e for m in ERROR_MARKERS)]
        if errs:
            a.reason = f"error: {errs[0][:120]}"
            break

        if closed_goal:
            # The goal was discharged and the model has now supplied a closing
            # command that Isabelle accepted without error.
            a.proved = True
            a.reason = "goal discharged, proof closed"
            break

        if goal_is_closed(state) and body.strip():
            # Goal gone but the proof not yet formally closed: the model must
            # still produce qed/done. Requiring this avoids crediting a proof
            # that was never terminated.
            closed_goal = True
        elif not state.strip():
            a.reason = ("no proof state" if step == 0
                        else "state vanished (command broke elaboration?)")
            break

        if step == cfg.max_steps:
            a.reason = f"step budget ({cfg.max_steps}) exhausted"
            break

        cands = tac.propose(head + body, state, cfg.num_candidates,
                            cfg.temperature)
        chosen = None
        for c in cands:
            if any(re.search(rf"\b{w}\b", c) for w in CHEATS):
                continue          # sorry/oops are accepted but prove nothing
            chosen = c
            break
        if chosen is None:
            a.reason = ("model proposed only sorry/oops" if cands
                        else "model produced nothing")
            break

        a.generated.append(chosen)
        body += "\n  " + chosen
        a.steps = step + 1

    a.seconds = round(time.monotonic() - t0, 1)
    return a

# --------------------------------------------------------------------------- #

async def main_async(cfg) -> int:
    afp = Path(cfg.afp).expanduser().resolve()
    if load_keyword_declarations:
        load_keyword_declarations(afp)

    wanted = {l.strip() for l in Path(cfg.entries).read_text().splitlines()
              if l.strip()}
    files = sorted(p for p in afp.glob("*/*.thy") if p.parent.name in wanted)
    log.info("%d theory files from %d held-out entries", len(files), len(wanted))

    harvested = load_harvested(Path(cfg.states))
    if harvested:
        log.info("%d files have harvested states to anchor against",
                 len(harvested))
    targets: list[Target] = []
    for thy in files:
        try:
            targets.extend(find_targets(
                thy, cfg.min_steps, cfg.max_steps,
                harvested.get(str(thy)) if harvested else None))
        except OSError:
            continue
    log.info("%d candidate theorems (proofs of %d-%d steps)",
             len(targets), cfg.min_steps, cfg.max_steps)

    # Prefer theorems in files that yielded many states during extraction.
    # A high count means the theory elaborates quickly and reliably, so the
    # attempt measures the model rather than the build system. Within that,
    # shuffle so the sample is not confined to a handful of entries.
    all_targets = list(targets)
    import random
    rng = random.Random(cfg.seed)
    rng.shuffle(targets)
    counts = {f: len(o) for f, o in harvested.items()} if harvested else {}
    targets.sort(key=lambda t: -counts.get(t.file, 0))
    if cfg.limit:
        # take from the top but spread across entries: round-robin by entry
        by_entry: dict[str, list[Target]] = {}
        for t in targets:
            by_entry.setdefault(t.entry, []).append(t)
        picked, i = [], 0
        while len(picked) < cfg.limit and any(by_entry.values()):
            for e in list(by_entry):
                if by_entry[e]:
                    picked.append(by_entry[e].pop(0))
                    if len(picked) >= cfg.limit:
                        break
            i += 1
            if i > cfg.limit * 2:
                break
        targets = picked
    if cfg.targets_in:
        keys = [tuple(x) for x in json.load(open(cfg.targets_in))]
        index = {(t.file, t.line): t for t in all_targets}
        missing = [k for k in keys if tuple(k) not in index]
        if missing:
            raise SystemExit(f"{len(missing)} targets from "
                             f"{cfg.targets_in} not found, e.g. {missing[:3]}")
        targets = [index[tuple(k)] for k in keys]
        log.info("loaded %d targets from %s", len(targets), cfg.targets_in)

    if cfg.targets_out:
        Path(cfg.targets_out).parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.targets_out, "w") as fh:
            json.dump([[t.file, t.line] for t in targets], fh, indent=1)
        log.info("wrote %d targets to %s", len(targets), cfg.targets_out)

    by_file: dict[str, list[Target]] = {}
    for t in targets:
        by_file.setdefault(t.file, []).append(t)
    log.info("attempting %d theorems across %d files", len(targets), len(by_file))

    tac = Tactician(cfg.model)
    Path(cfg.out).parent.mkdir(parents=True, exist_ok=True)
    out = Path(cfg.out).open("w", encoding="utf-8")
    results: list[Attempt] = []

    for thy_path, group in by_file.items():
        thy = Path(thy_path)
        original = thy.read_text(encoding="utf-8", errors="replace")
        options = ["-l", cfg.logic, "-d", str(afp),
                   "-o", "vscode_html_output=true"]
        sess = IsabelleSession(cfg.isabelle, options,
                               Path(cfg.log_dir) / f"{thy.stem}.log")
        try:
            await sess.start(thy)
        except Exception as e:
            # Record rather than skip. A dropped theorem shrinks the
            # denominator, so two runs over the same targets would report
            # different totals and cease to be comparable -- an
            # infrastructure difference presenting as a difference in
            # capability.
            log.warning("%s: Isabelle failed to start (%s)", thy.name, e)
            for t in group:
                a = Attempt(file=t.file, entry=t.entry, line=t.line,
                            statement=t.statement, reference=t.reference,
                            reason=f"isabelle failed to start: {e}"[:120])
                results.append(a)
                out.write(json.dumps(asdict(a), ensure_ascii=False) + "\n")
            out.flush()
            continue
        # Wait for the theory to elaborate before the first attempt: a large
        # file in a heavy session can take minutes, and a premature read
        # returns an empty state that looks like a model failure.
        warm = await sess.warm_up(original, group[0], cfg.warmup)
        if not warm:
            log.warning("%s: no state after %.0fs warm-up; recording %d "
                        "theorems as unattempted", thy.name, cfg.warmup,
                        len(group))
            for t in group:
                a = Attempt(file=t.file, entry=t.entry, line=t.line,
                            statement=t.statement, reference=t.reference,
                            reason=f"theory did not elaborate within "
                                   f"{cfg.warmup:.0f}s warm-up")
                results.append(a)
                out.write(json.dumps(asdict(a), ensure_ascii=False) + "\n")
            out.flush()
            await sess.stop()
            continue
        log.info("%s: elaborated, attempting %d theorems", thy.name, len(group))

        try:
            for t in group:
                try:
                    a = await asyncio.wait_for(
                        attempt_proof(sess, tac, original, t, cfg),
                        timeout=cfg.theorem_timeout)
                except asyncio.TimeoutError:
                    a = Attempt(file=t.file, entry=t.entry, line=t.line,
                                statement=t.statement, reference=t.reference,
                                reason="timeout")
                results.append(a)
                out.write(json.dumps(asdict(a), ensure_ascii=False) + "\n")
                out.flush()
                mark = "PROVED" if a.proved else "     -"
                log.info("%s %s:%d  %d steps %.0fs  %s", mark, thy.name,
                         a.line, a.steps, a.seconds, a.statement[:60])
        finally:
            await sess.stop()
            # restore: we only ever edited the in-memory document, but be safe
            thy.write_text(original, encoding="utf-8") if cfg.write_back else None

    out.close()
    n = len(results)
    p = sum(r.proved for r in results)
    print(f"\n{'='*56}\nproof completion: {p}/{n} = {p/max(1,n):.4f}\n{'='*56}")
    from collections import Counter
    for reason, c in Counter(r.reason for r in results if not r.proved
                             ).most_common(8):
        print(f"  {c:>5}  {reason[:70]}")
    if p:
        s = [r.steps for r in results if r.proved]
        print(f"\nproved in {sum(s)/len(s):.1f} steps on average")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--afp", required=True)
    ap.add_argument("--entries", required=True)
    ap.add_argument("--isabelle", required=True)
    ap.add_argument("--out", default="results/completion.jsonl")
    ap.add_argument("--log-dir", default="data/logs/completion")
    ap.add_argument("--logic", default="HOL")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--min-steps", type=int, default=1)
    ap.add_argument("--max-steps", type=int, default=12,
                    help="also the generation budget per theorem")
    ap.add_argument("--num-candidates", type=int, default=1)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--states", default="data/test_merged/states.jsonl",
                    help="harvested states, used to restrict attempts to "
                         "theorem positions known to elaborate")
    ap.add_argument("--warmup", type=float, default=180.0,
                    help="seconds to wait for a theory to elaborate before "
                         "the first attempt in it")
    ap.add_argument("--settle", type=float, default=20.0)
    ap.add_argument("--theorem-timeout", type=float, default=300.0)
    ap.add_argument("--write-back", action="store_true",
                    help="rewrite the source file on disk (off by default: "
                         "edits are made to the in-memory LSP document only, "
                         "so the corpus is never modified)")
    ap.add_argument("--targets-out", default=None,
                    help="write the selected theorems to this file")
    ap.add_argument("--targets-in", default=None,
                    help="read the theorem list from a file written by "
                         "--targets-out. Use this to guarantee that two "
                         "models are evaluated on an IDENTICAL set: sampling "
                         "with the same seed is not sufficient if the "
                         "candidate pool differs between runs.")
    ap.add_argument("--seed", type=int, default=20260825)
    ap.add_argument("-v", "--verbose", action="store_true")
    cfg = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if cfg.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    return asyncio.run(main_async(cfg))


if __name__ == "__main__":
    sys.exit(main())
