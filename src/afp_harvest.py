#!/usr/bin/env python3
"""
afp_harvest.py -- harvest (proof state, tactic) pairs from AFP theory files
by driving isabelle-emacs over LSP and reading PIDE/dynamic_output.

Pipeline
--------
  1. discover .thy files under  afp/thys/<Session>/*.thy
  2. compute caret probe positions inside each file
       --mode commands : end of every Isar command  (fast, default)
       --mode tokens   : after every whitespace token (exhaustive, slow)
  3. for each position: send caret_update, wait for PIDE/dynamic_output to settle
  4. record every position where the dynamic output CHANGED  -> states.jsonl
  5. pair consecutive states with the command between them  -> transitions.jsonl

Output records
--------------
states.jsonl
  {"file","session","line","character","probe","command","state"}
transitions.jsonl
  {"file","session","line","state_before","tactic","state_after"}

The transition rows are the training rows: state_before is the goal the model
sees, tactic is the command it must emit, state_after is the resulting goal.
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
from typing import Iterable, Iterator, Optional

try:
    from panel_text import panel_html_to_text
except ImportError:  # pragma: no cover
    raise SystemExit(
        "panel_text.py must sit next to afp_harvest.py "
        "(it converts Isabelle's HTML panel output back to plain text)")

log = logging.getLogger("afp_harvest")

STATE_INIT = "PIDE/state_init"
STATE_OUTPUT = "PIDE/state_output"


# --------------------------------------------------------------------------- #
# 1. Isabelle symbol decoding (optional, for readable goals)
# --------------------------------------------------------------------------- #

_SYMBOL_RE = re.compile(r"\\<\^?[A-Za-z][A-Za-z0-9_']*>")


def load_symbol_table(isabelle_home: Optional[str]) -> dict[str, str]:
    """Parse $ISABELLE_HOME/etc/symbols into {'\\<And>': '⋀', ...}."""
    if not isabelle_home:
        return {}
    path = Path(isabelle_home) / "etc" / "symbols"
    if not path.is_file():
        log.warning("no symbol table at %s; leaving \\<...> escapes intact", path)
        return {}
    table: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        sym = parts[0]
        for field_ in parts[1:]:
            if field_.startswith("code:"):
                try:
                    table[sym] = chr(int(field_.split(":", 1)[1], 16))
                except ValueError:
                    pass
                break
    log.info("loaded %d Isabelle symbols", len(table))
    return table


def decode_symbols(text: str, table: dict[str, str]) -> str:
    if not table or not text:
        return text
    return _SYMBOL_RE.sub(lambda m: table.get(m.group(0), m.group(0)), text)


# --------------------------------------------------------------------------- #
# 2. Theory scanning: tokens, commands, caret positions
# --------------------------------------------------------------------------- #

ISAR_COMMANDS = {
    # theory / specification level
    "theory", "imports", "begin", "end", "context", "locale", "class",
    "instantiation", "instance", "interpretation", "sublocale", "bundle",
    "definition", "abbreviation", "fun", "function", "primrec", "primcorec",
    "termination", "datatype", "codatatype", "record", "type_synonym",
    "typedef", "typedecl", "consts", "axiomatization", "declare", "declaration",
    "lemmas", "notation", "no_notation", "syntax", "translations",
    "inductive", "inductive_set", "coinductive", "coinductive_set",
    "lift_definition", "setup_lifting", "code_datatype", "export_code",
    "sledgehammer_params", "method", "ML", "ML_file", "setup", "local_setup",
    # document markup -- not useful themselves, but they must terminate the
    # span of the preceding proof command or the caret drifts into prose
    "chapter", "section", "subsection", "subsubsection", "paragraph",
    "subparagraph", "text", "txt", "text_raw", "abstract",
    # goal openers
    "lemma", "theorem", "corollary", "proposition", "schematic_goal",
    # proof body
    "proof", "qed", "apply", "apply_end", "by", "done", "next", "oops",
    "sorry", "show", "showing", "thus", "have", "hence", "obtain", "fix",
    "assume", "presume", "define", "let", "note", "also", "finally",
    "moreover", "ultimately", "using", "unfolding", "with", "from", "then",
    "case", "consider", "supply", "include", "including", "subgoal",
    "defer", "prefer", "back", "term", "value", "typ", "thm", "print_statement",
}

# Commands that OPEN a new goal. Their state is the initial goal of a fresh
# proof, so they can never be the tactic of a transition -- pairing one with
# the previous row's state splices the end of one proof onto the start of the
# next, which is silent garbage in the training data.
GOAL_OPENERS = {
    "lemma", "theorem", "corollary", "proposition", "schematic_goal",
    "definition", "fun", "function", "primrec", "primcorec", "termination",
    "datatype", "inductive", "inductive_set", "coinductive", "instance",
    "instantiation", "interpretation", "sublocale", "lift_definition",
    "typedef", "abbreviation",
}

# Commands that we never want to emit as a "tactic" in the dataset.
NON_TACTIC_COMMANDS = {
    "theory", "imports", "begin", "end", "context", "section", "subsection",
    "subsubsection", "text", "ML", "ML_file", "declare", "notation",
    "no_notation", "syntax", "translations", "export_code", "term", "value",
    "typ", "thm", "print_statement",
}

_OPEN_CARTOUCHE = "\u2039"   # ‹
_CLOSE_CARTOUCHE = "\u203a"  # ›


@dataclass(frozen=True)
class Tok:
    start: int
    end: int
    kind: str          # word | string | cartouche | comment
    text: str


def scan(text: str) -> list[Tok]:
    """Lexer that is comment-, string- and cartouche-aware.

    Good enough to find command boundaries without a full Isar grammar.
    Anything inside (* *), "..." or ‹...› can never start a command.
    """
    toks: list[Tok] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue

        if text.startswith("(*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("(*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*)", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            toks.append(Tok(i, j, "comment", text[i:j]))
            i = j
            continue

        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            toks.append(Tok(i, j, "string", text[i:j]))
            i = j
            continue

        if c == _OPEN_CARTOUCHE or text.startswith("\\<open>", i):
            depth, j = 1, i + (1 if c == _OPEN_CARTOUCHE else 7)
            while j < n and depth:
                if text[j] == _OPEN_CARTOUCHE or text.startswith("\\<open>", j):
                    depth += 1
                    j += 1 if text[j] == _OPEN_CARTOUCHE else 7
                elif text[j] == _CLOSE_CARTOUCHE or text.startswith("\\<close>", j):
                    depth -= 1
                    j += 1 if text[j] == _CLOSE_CARTOUCHE else 8
                else:
                    j += 1
            toks.append(Tok(i, j, "cartouche", text[i:j]))
            i = j
            continue

        j = i
        while j < n and not text[j].isspace():
            if text.startswith("(*", j) or text[j] in ('"', _OPEN_CARTOUCHE):
                break
            j += 1
        if j == i:
            j = i + 1
        toks.append(Tok(i, j, "word", text[i:j]))
        i = j

    return toks


class LineIndex:
    """Byte-offset -> (line, character) with a selectable character encoding."""

    def __init__(self, text: str, encoding: str = "utf16") -> None:
        self.text = text
        self.encoding = encoding
        self.line_starts = [0]
        for m in re.finditer(r"\n", text):
            self.line_starts.append(m.end())

    def position(self, offset: int) -> tuple[int, int]:
        # binary search for the line
        lo, hi = 0, len(self.line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        start = self.line_starts[lo]
        prefix = self.text[start:offset]
        if self.encoding == "utf16":
            char = len(prefix.encode("utf-16-le")) // 2
        else:
            char = len(prefix)
        return lo, char  # both 0-indexed, as the LSP wants


@dataclass
class Probe:
    """One caret placement."""
    offset: int
    line: int
    character: int
    probe_text: str      # the token / command text the caret sits at the end of
    command: str         # the Isar command keyword this position belongs to
    prefix: str = ""     # theory text up to the caret
    continuation: str = ""   # theory text after the caret, to the proof's end
    in_proof: bool = False
    remaining: int = -1  # commands left in the proof after the caret


# Commands that can open a block of proof text.
PROOF_OPENERS = {
    "lemma", "theorem", "corollary", "proposition", "schematic_goal",
    "function", "termination", "instance", "interpretation", "sublocale",
    "lift_definition", "primcorec",
}


def proof_blocks(text: str) -> list[tuple[int, int]]:
    """Character spans of each proof, from its opener to the END of its
    closing command (arguments included).

    Scoping `continuation` to the proof rather than to the file is what makes
    it empty exactly when the proof is finished -- the signal the model needs
    in order to learn when to stop.

    Nesting is tracked so that `by` closing an inner `have` is not mistaken
    for the end of the enclosing proof: `by`/`done` only close at depth 0, and
    `qed` closes only when it brings the depth back to 0.
    """
    blocks: list[tuple[int, int]] = []
    start: Optional[int] = None
    depth = 0
    prev_end: Optional[int] = None      # end of the last command span seen

    for kw, s, e in command_spans(text):
        if start is None:
            if kw in PROOF_OPENERS:
                start, depth = s, 0
            prev_end = e
            continue

        prev_end_before = prev_end
        prev_end = e

        if kw == "proof":
            depth += 1
        elif kw == "qed":
            depth -= 1
            if depth <= 0:
                blocks.append((start, e))
                start, depth = None, 0
        elif kw in ("done", "by") and depth == 0:
            blocks.append((start, e))
            start = None
        elif kw in ("oops", "sorry"):
            blocks.append((start, e))
            start, depth = None, 0
        elif kw in PROOF_OPENERS and depth == 0:
            # The previous block never closed with by/done/qed -- it was a
            # one-liner such as `lift_definition ... is "f" .` or a
            # `definition`/`instance` with no proof. Close it at the END of the
            # last command span, not at the start of this one: block ends must
            # coincide with caret positions, or every such block is orphaned.
            blocks.append((start, prev_end_before if prev_end_before is not None else s))
            start, depth = s, 0

    if start is not None:
        blocks.append((start, prev_end if prev_end is not None else len(text)))
    return blocks


def block_for(offset: int, blocks: list[tuple[int, int]]) -> Optional[tuple[int, int]]:
    for s, e in blocks:
        if s <= offset <= e:
            return (s, e)
    return None


def attach_context(text: str, probes: list[Probe], scope: str = "proof") -> None:
    """Fill in prefix and continuation on each probe, in place.

    scope="proof" keeps prefix from the proof's opener (compact, and the
    continuation ends with the proof). scope="file" uses the whole file, which
    is complete but repeats the file once per row.
    """
    blocks = proof_blocks(text)
    spans = command_spans(text)
    for p in probes:
        blk = block_for(p.offset, blocks)
        if blk is None:
            p.prefix = text[:p.offset] if scope == "file" else ""
            p.continuation = ""
            p.in_proof = False
            p.remaining = -1
            continue
        p.in_proof = True
        s, e = blk
        p.remaining = sum(1 for _, cs, ce in spans if p.offset < ce <= e)
        p.prefix = text[(0 if scope == "file" else s):p.offset]
        p.continuation = text[p.offset:e]


def probes_by_token(text: str, idx: LineIndex) -> list[Probe]:
    """Caret after every whitespace-delimited token. Exhaustive and slow."""
    out: list[Probe] = []
    current_cmd = ""
    for tok in scan(text):
        if tok.kind == "comment":
            continue
        kw = command_keyword(tok.text) if tok.kind == "word" else None
        if kw:
            current_cmd = kw
        line, char = idx.position(tok.end)
        out.append(Probe(tok.end, line, char, tok.text, current_cmd))
    return out


_KEYWORD_HEAD = re.compile(r"^([A-Za-z][A-Za-z0-9_']*)")


def command_keyword(tok_text: str) -> Optional[str]:
    """The Isar command a word token starts, or None.

    Isabelle does not require a space before a delimiter, so `proof(cases x)`,
    `by(auto)` and `apply(induct xs)` are all legal and all lex as a single
    whitespace-delimited token. Matching the token exactly against the keyword
    list misses them -- and missing a `proof` throws off proof-block nesting,
    which then lets a later `by` close the wrong block.
    """
    m = _KEYWORD_HEAD.match(tok_text)
    if not m:
        return None
    kw = m.group(1)
    if kw not in ISAR_COMMANDS:
        return None
    rest = tok_text[m.end():]
    if rest and rest[0] not in "([{<\"'":
        return None                    # e.g. an identifier like `proofs`
    return kw


def command_spans(text: str) -> list[tuple[str, int, int]]:
    """(keyword, start, end) for each Isar command span.

    A span runs from a command keyword to just before the next one, with
    trailing comments trimmed. Shared by probes_by_command and proof_blocks so
    that a block's end and a caret position agree exactly -- they must, or
    `by (auto simp: foo)` ends its block after `by` and the caret at the end of
    the command falls outside every block.
    """
    toks = scan(text)
    starts = [(k, command_keyword(t.text)) for k, t in enumerate(toks)
              if t.kind == "word" and command_keyword(t.text)]
    out: list[tuple[str, int, int]] = []
    for n, (k, kw) in enumerate(starts):
        stop = starts[n + 1][0] if n + 1 < len(starts) else len(toks)
        span = [t for t in toks[k:stop] if t.kind != "comment"]
        if not span:
            continue
        out.append((kw, span[0].start, span[-1].end))
    return out


def probes_by_command(text: str, idx: LineIndex) -> list[Probe]:
    """Caret at the end of every Isar command span."""
    out: list[Probe] = []
    for kw, s, e in command_spans(text):
        line, char = idx.position(e)
        out.append(Probe(e, line, char, " ".join(text[s:e].split()), kw))
    return out


# --------------------------------------------------------------------------- #
# 3. AFP session layout (ROOT files)
# --------------------------------------------------------------------------- #

_SESSION_RE = re.compile(
    r'^\s*session\s+"?([\w.\-]+)"?\s*(?:\([^)]*\))?\s*(?:=\s*"?([\w.\-]+)"?\s*\+)?',
    re.MULTILINE,
)


@dataclass
class SessionInfo:
    name: str
    parent: str
    directory: Path


def parse_roots(thys_dir: Path) -> dict[str, SessionInfo]:
    """Best-effort scan of AFP ROOT files: session name -> parent + dir."""
    sessions: dict[str, SessionInfo] = {}
    for root in thys_dir.glob("*/ROOT"):
        try:
            body = root.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name, parent in _SESSION_RE.findall(body):
            sessions[name] = SessionInfo(name, parent or "HOL", root.parent)
    log.info("parsed %d sessions from ROOT files", len(sessions))
    return sessions


def session_for(thy: Path, sessions: dict[str, SessionInfo]) -> SessionInfo:
    """AFP convention: afp/thys/<Session>/<Theory>.thy."""
    guess = thy.parent.name
    if guess in sessions:
        return sessions[guess]
    for info in sessions.values():
        if info.directory == thy.parent:
            return info
    return SessionInfo(guess, "HOL", thy.parent)


# --------------------------------------------------------------------------- #
# 4. Driving Isabelle over LSP
# --------------------------------------------------------------------------- #
#
# API shape of isabelle_lsp_client 0.0.2 (verified against the installed
# package, not assumed):
#
#   * IsabelleProcess(clientHandler).run(args) BLOCKS until Isabelle exits.
#     It builds the real IsabelleClient internally; there is no client object
#     to reach from outside. All work happens in callbacks.
#   * write_loop() initialises, waits for the heap, opens args["theory"] as a
#     Document, then calls clientHandler.on_start(). That callback is the
#     signal that the theory is loaded -- no timing guesswork needed.
#   * Callbacks are async and take (document, response, timestamp).
#   * response["params"]["content"] is the proof state; ["decorations"] is
#     markup only and is ignored.
#   * document.move_caret(line, character) also keeps document.caret_position
#     in sync, so prefer it over raw caret_update.

class RawRequest:
    """Minimal stand-in for lsp_client's BaseRequest.

    LSPClient.request() only sets `.id` and calls
    `.model_dump(exclude_none=True)`, so this avoids depending on the
    library's pydantic model layout.
    """

    def __init__(self, method: str, params=None) -> None:
        self.jsonrpc = "2.0"
        self.id = None
        self.method = method
        self.params = params

    def model_dump(self, exclude_none: bool = True) -> dict:
        d = {"jsonrpc": self.jsonrpc, "id": self.id,
             "method": self.method, "params": self.params}
        return {k: v for k, v in d.items() if not (exclude_none and v is None)}


@dataclass
class StateRow:
    file: str
    session: str
    line: int
    character: int
    probe: str
    command: str
    state: str
    # prefix + state -> continuation is the training triple. continuation is
    # scoped to the enclosing proof, so it is empty exactly when the proof is
    # finished, which is how the model learns to stop.
    prefix: str = ""
    continuation: str = ""
    # False for rows outside any proof (theory header, text blocks). Their
    # continuation is empty too, but that is NOT a proof-finished signal --
    # filter these out before training.
    in_proof: bool = False
    # Commands left in the proof after this caret. 0 means the proof is over;
    # 1 means only the closing command (`done`/`qed`/`by`) remains -- which is
    # the real stop signal, because Isabelle emits NO state at the position
    # that closes a proof, so a literally empty continuation never occurs in
    # harvested data.
    remaining: int = -1
    offset: int = 0       # char offset of the caret, to recover full context
    output: str = ""      # output-panel text (hints, errors); state is primary


class Harvester:
    """Walks the caret through one theory and collects proof states.

    Instances are handed to ClientHandler as the on_start and
    on_dynamic_output callbacks; the whole probe loop runs inside on_start,
    exactly as the package's own auto_sledge example does.
    """

    def __init__(self, probes: list[Probe], cfg: argparse.Namespace,
                 symbols: dict[str, str], thy: Path, session_name: str,
                 raw_dump: Optional[Path] = None) -> None:
        self.probes = probes
        self.cfg = cfg
        self.symbols = symbols
        self.thy = thy
        self.session_name = session_name
        self.raw_dump = raw_dump

        self.rows: list[StateRow] = []
        self.done = asyncio.Event()
        self.error: Optional[BaseException] = None

        self.state_id = None
        self._latest: Optional[str] = None
        self._output: str = ""
        self._changed = asyncio.Event()
        self._count = 0
        self._raw_fh = None
        self._t0 = time.monotonic()

    # -- callbacks (async, 3-arg: this is what ClientHandler.handle calls) -- #

    def _dump(self, kind: str, document, content: str, changed: bool) -> None:
        if self._raw_fh is None:
            return
        self._raw_fh.write(json.dumps({
            "t": round(time.monotonic() - self._t0, 4),
            "n": self._count,
            "kind": kind,
            "changed": changed,
            "caret": list(getattr(document, "caret_position", ()) or ()),
            "content": content,
        }, ensure_ascii=False) + "\n")
        self._raw_fh.flush()

    async def on_state_output(self, document, response: dict,
                              timestamp: int) -> None:
        """PIDE/state_output -- the STATE panel. This is the proof state.

        Distinct from PIDE/dynamic_output, which is the OUTPUT panel and
        carries solve_direct hints and the finished theorem, but is empty at
        exactly the apply steps we care about.

        Content is HTML because vscode_html_output must be true: the
        plain-text branch of Pretty_Text_Panel emits a decoration object that
        fails to serialise ("Bad JSON value: ...Lambda") in this fork.
        Pretty.formatted has already wrapped the lines, so the HTML's text
        content is the exact plain-text state.
        """
        content = (response.get("params") or {}).get("content")
        if content is None:
            return
        self._count += 1
        text = panel_html_to_text(content)
        changed = text != self._latest
        self._dump("state", document, content, changed)
        if changed:
            self._latest = text
            self._changed.set()

    async def on_dynamic_output(self, document, response: dict,
                                timestamp: int) -> None:
        """Output panel: recorded alongside the state, never as the state."""
        content = (response.get("params") or {}).get("content")
        if content is None:
            return
        text = panel_html_to_text(content)
        self._dump("output", document, content, text != self._output)
        self._output = text

    async def on_start(self, document, **kwargs) -> None:
        """Theory is open and processed. Run the whole probe sweep here."""
        try:
            if self.raw_dump is not None:
                self.raw_dump.parent.mkdir(parents=True, exist_ok=True)
                self._raw_fh = self.raw_dump.open("w", encoding="utf-8")
            self._t0 = time.monotonic()
            await self._init_state_panel(document)
            await self._sweep(document)
        except BaseException as e:                     # noqa: BLE001
            self.error = e
        finally:
            if self._raw_fh is not None:
                self._raw_fh.close()
                self._raw_fh = None
            self.done.set()

    async def _init_state_panel(self, document) -> None:
        """Send the PIDE/state_init REQUEST and keep the state_id.

        Without this no state_output is ever sent. The panel then sets
        auto_update_enabled = true and refreshes on Session.Caret_Focus, so
        moving the caret is enough -- no explicit state_update is needed.
        """
        lsp = getattr(document, "_mock_lsp", None)
        if lsp is None:
            isabelle = getattr(document, "isabelle", None)
            for name in ("lspClient", "lsp_client", "client", "_lspClient"):
                c = getattr(isabelle, name, None)
                if c is not None and hasattr(c, "request"):
                    lsp = c
                    break
        if lsp is None:
            raise RuntimeError("no LSPClient reachable from document.isabelle")

        result = await lsp.request(RawRequest(STATE_INIT), timeout=60)
        self.state_id = (result or {}).get("state_id")
        if self.state_id is None:
            raise RuntimeError(f"{STATE_INIT} returned no state_id: {result!r}")
        log.info("state panel initialised (state_id=%s)", self.state_id)

    # -- the sweep ---------------------------------------------------------- #

    async def _sweep(self, document) -> None:
        cfg = self.cfg
        trace = getattr(cfg, "trace", False)
        last_state: Optional[str] = None

        for p in self.probes:
            t = time.monotonic()
            state = await self._probe(document, p)
            dt = time.monotonic() - t

            if state is None:
                if trace:
                    print(f"--- L{p.line+1}:{p.character} [{p.command}] "
                          f"{p.probe_text[:55]}  ({dt:.2f}s)  <no output>")
                continue
            if state == last_state:
                if trace:
                    print(f"--- L{p.line+1}:{p.character} [{p.command}] "
                          f"{p.probe_text[:55]}  ({dt:.2f}s)  <unchanged>")
                continue

            last_state = state
            self.rows.append(StateRow(
                file=str(self.thy),
                session=self.session_name,
                line=p.line + 1,                       # 1-indexed for humans
                character=p.character,
                probe=p.probe_text,
                command=p.command,
                state=decode_symbols(state, self.symbols),
                prefix=p.prefix,
                continuation=p.continuation,
                in_proof=p.in_proof,
                remaining=p.remaining,
                offset=p.offset,
                output=self._output,
            ))
            if trace:
                print(f"\n=== L{p.line+1}:{p.character} [{p.command}] "
                      f"{p.probe_text[:55]}  ({dt:.2f}s)")
                for ln in self.rows[-1].state.splitlines()[:cfg.trace_lines]:
                    print("    " + ln)
            elif cfg.verbose:
                head = self.rows[-1].state.splitlines()[0] if self.rows[-1].state else ""
                log.debug("L%-5d %-20s -> %s", p.line + 1, p.probe_text[:20], head)

    async def _probe(self, document, p: Probe) -> Optional[str]:
        """Move the caret, then wait for the output to settle.

        Returns None if nothing arrived within --settle. Waits up to `settle`
        for the first message, but returns early once output has been stable
        for `quiet`: Isabelle re-prints the goal repeatedly while a command is
        still elaborating, so the first message is often stale.
        """
        settle = self.cfg.settle
        quiet = self.cfg.quiet

        self._changed.clear()
        before = self._latest
        await document.move_caret(p.line, p.character)

        deadline = time.monotonic() + settle
        got_new = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._changed.wait(),
                                       timeout=min(remaining, quiet))
            except asyncio.TimeoutError:
                if got_new:
                    break                              # settled
                continue                               # keep waiting
            self._changed.clear()
            got_new = True

        if self._latest is None:
            return None
        if not got_new and self._latest == before:
            return None                                # same command span
        return self._latest


class MockDocument:
    """Stand-in for isabelle_lsp_client Document, used by --mock."""

    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.caret_position = (0, 0)
        self.lines: list[str] = []
        self.calls: list[tuple[int, int]] = []
        self._harvester: Optional[Harvester] = None

    def bind(self, harvester: Harvester) -> None:
        self._harvester = harvester

    async def move_caret(self, line: int = 0, character: int = 0) -> None:
        self.caret_position = (line, character)
        self.calls.append((line, character))
        if self._harvester is None:
            return
        if line % 7 == 6:                              # exercise the None path
            return
        html = (f'<pre class="source"><span class="keyword1">proof</span> '
                f'(prove)\ngoal (1 subgoal):\n 1. '
                f'mock_goal_at_line_{line+1} <span class="main">&amp;</span>'
                f'</pre>')
        await self._harvester.on_state_output(self, {"params": {"content": html}}, 0)


class _MockLSP:
    """Answers PIDE/state_init for --mock."""

    async def request(self, request, timeout=None):
        return {"state_id": -1}


def isabelle_uri(p: Path) -> str:
    """Match write_loop's convention exactly: 'file://' + abspath, unencoded.

    Path.as_uri() percent-encodes, which would not match the URI Isabelle was
    given and would silently produce no output for any path with a space.
    """
    return "file://" + os.path.abspath(str(p))


# --------------------------------------------------------------------------- #
# 5. Harvest one file
# --------------------------------------------------------------------------- #

async def harvest_file(thy: Path, cfg: argparse.Namespace,
                       sessions: dict[str, SessionInfo],
                       symbols: dict[str, str]) -> list[StateRow]:
    text = thy.read_text(encoding="utf-8", errors="replace")
    idx = LineIndex(text, cfg.offset_encoding)
    probes = (probes_by_command(text, idx) if cfg.mode == "commands"
              else probes_by_token(text, idx))
    if not probes:
        return []
    attach_context(text, probes, cfg.prefix_scope)

    info = session_for(thy, sessions)
    options = ["-l", cfg.logic or info.parent]
    if cfg.session_dirs:
        for d in cfg.session_dirs:
            options += ["-d", d]
    extra = list(getattr(cfg, "isabelle_option", []) or [])
    # The library hardcodes -o vscode_html_output=false, but that code path
    # emits a decoration object Isabelle cannot serialise ("Bad JSON value:
    # ...Lambda") in Isabelle2025-2-vsce, so no panel output ever arrives.
    # A later -o wins, so appending true here repairs it.
    if not any(o.startswith("vscode_html_output") for o in extra):
        extra.append("vscode_html_output=true")
    for opt in extra:
        options += ["-o", opt]

    raw_dump = (Path(cfg.log_dir) / f"{thy.stem}.dynamic_output.jsonl"
                if getattr(cfg, "dump_raw", False) else None)
    harvester = Harvester(probes, cfg, symbols, thy, info.name, raw_dump)

    if getattr(cfg, "mock", False):
        doc = MockDocument(isabelle_uri(thy))
        doc._mock_lsp = _MockLSP()
        doc.bind(harvester)
        await harvester.on_start(doc)
        if harvester.error:
            raise harvester.error
        log.info("%s: %d probes -> %d states (mock)",
                 thy.name, len(probes), len(harvester.rows))
        return harvester.rows

    from isabelle_lsp_client import ClientHandler, IsabelleProcess  # type: ignore

    handler = ClientHandler()
    handler.register_on_start(harvester.on_start)
    handler.register(STATE_OUTPUT, harvester.on_state_output)
    handler.register_on_dynamic_output(harvester.on_dynamic_output)

    args = {
        "exec": cfg.isabelle,
        "options": options,
        "log_path": str(Path(cfg.log_dir) / f"{thy.stem}.isabelle.log"),
        "theory": str(thy.resolve()),
        "startup_timeout": cfg.startup_timeout,
    }
    log.info("%s: %d probes, isabelle %s", thy.name, len(probes),
             " ".join(options))

    process = IsabelleProcess(handler)
    run_task = asyncio.create_task(process.run(args))
    done_task = asyncio.create_task(harvester.done.wait())

    try:
        # run() never returns on its own; whichever finishes first decides.
        # If run_task wins, Isabelle died before the sweep completed.
        await asyncio.wait({run_task, done_task},
                           return_when=asyncio.FIRST_COMPLETED)
        if run_task.done() and not harvester.done.is_set():
            exc = run_task.exception()
            raise exc if exc else RuntimeError(
                "Isabelle exited before on_start fired -- check "
                f"{args['log_path']} and that the '{options[1]}' heap is built")
    finally:
        for t in (run_task, done_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(run_task, done_task, return_exceptions=True)

    if harvester.error:
        raise harvester.error

    log.info("%s: %d probes -> %d distinct states (%d messages)", thy.name,
             len(probes), len(harvester.rows), harvester._count)
    return harvester.rows
# --------------------------------------------------------------------------- #
# 6. Transitions
# --------------------------------------------------------------------------- #

def build_transitions(rows: Iterable[StateRow]) -> Iterator[dict]:
    """Pair consecutive states within a file: (state_before, tactic, state_after).

    Row i-1 holds the goal as it stood before row i's command ran; row i's
    probe text is the command that transformed it. That is the training pair.
    """
    rows = list(rows)
    for a, b in zip(rows, rows[1:]):
        if a.file != b.file:
            continue
        if b.command in NON_TACTIC_COMMANDS or not b.command:
            continue
        if b.command in GOAL_OPENERS:
            continue          # new proof: not a step from a.state
        if not (a.in_proof and b.in_proof):
            continue          # outside any proof: not a proof step
        if not a.state.strip():
            continue
        yield {
            "file": b.file,
            "session": b.session,
            "line": b.line,
            "prefix": a.prefix,          # text the model has seen so far
            "state_before": a.state,     # goal it must act on
            "tactic": b.probe,           # what the human wrote next
            "command": b.command,
            "state_after": b.state,
            "continuation": b.continuation,
            "remaining": b.remaining,
            "closes_proof": b.remaining == 0,
            "last_step": b.remaining <= 1,
        }


# --------------------------------------------------------------------------- #
# 7. CLI
# --------------------------------------------------------------------------- #

def discover(afp: Path, limit: Optional[int], include: Optional[str]) -> list[Path]:
    files = sorted(p for p in afp.glob("*/*.thy") if p.is_file())
    if include:
        rx = re.compile(include)
        files = [p for p in files if rx.search(str(p))]
    return files[:limit] if limit else files


def probe_api() -> None:
    """Dump the real API surface of the installed client, then exit."""
    try:
        import isabelle_lsp_client as m  # type: ignore
    except ImportError as e:
        print(f"cannot import isabelle_lsp_client: {e}")
        return
    print("module:", getattr(m, "__file__", "?"),
          "version:", getattr(m, "__version__", "?"))
    for name in dir(m):
        if name.startswith("_"):
            continue
        obj = getattr(m, name)
        if isinstance(obj, type):
            members = [a for a in dir(obj) if not a.startswith("_")]
            print(f"\n{name}:")
            for a in members:
                print("   ", a)


async def main_async(cfg: argparse.Namespace) -> int:
    afp = Path(cfg.afp).expanduser().resolve()
    if not afp.is_dir():
        log.error("not a directory: %s", afp)
        return 2

    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    out_dir = Path(cfg.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    sessions = parse_roots(afp)
    symbols = load_symbol_table(cfg.isabelle_home or os.environ.get("ISABELLE_HOME"))
    files = discover(afp, cfg.limit, cfg.include)
    log.info("%d theory files to process", len(files))

    # Append-only checkpoint. A JSON array cannot be appended to safely, so
    # the run writes JSONL as it goes and converts at the end; if the run dies
    # 400 theories in, --resume picks up from here instead of starting over.
    ckpt_path = out_dir / "states.checkpoint.jsonl"
    done: set[str] = set()
    if cfg.resume and ckpt_path.exists():
        with ckpt_path.open() as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["file"])
                except Exception:
                    pass
        log.info("resuming: %d files already harvested", len(done))
    elif not cfg.resume and ckpt_path.exists():
        ckpt_path.unlink()

    all_rows: list[StateRow] = []
    with ckpt_path.open("a", encoding="utf-8") as fh:
        for thy in files:
            if str(thy) in done:
                continue
            try:
                rows = await asyncio.wait_for(
                    harvest_file(thy, cfg, sessions, symbols),
                    timeout=cfg.file_timeout,
                )
            except asyncio.TimeoutError:
                log.warning("%s: timed out after %ds", thy.name, cfg.file_timeout)
                continue
            except Exception as e:
                log.warning("%s: failed (%s: %s)", thy.name, type(e).__name__, e)
                continue
            for r in rows:
                fh.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
            fh.flush()
            all_rows.extend(rows)

    # Rebuild from the full checkpoint so --resume produces a complete dataset.
    rows: list[StateRow] = []
    with ckpt_path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(StateRow(**json.loads(line)))
            except Exception:
                pass
    transitions = list(build_transitions(rows))

    written = write_dataset(out_dir, "states", [asdict(r) for r in rows], cfg.format)
    written += write_dataset(out_dir, "transitions", transitions, cfg.format)
    for p in written:
        log.info("wrote %s", p)
    log.info("%d states, %d transitions", len(rows), len(transitions))
    return 0


def write_dataset(out_dir: Path, name: str, records: list[dict],
                  fmt: str) -> list[Path]:
    """Serialise one table. JSON array by default, JSONL on request."""
    written: list[Path] = []
    if fmt in ("json", "both"):
        p = out_dir / f"{name}.json"
        with p.open("w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        written.append(p)
    if fmt in ("jsonl", "both"):
        p = out_dir / f"{name}.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        written.append(p)
    return written


SELF_TEST_THY = '''theory Sanity
  imports Main
begin

(* this comment mentions lemma apply by qed and must be ignored *)

lemma rev_rev [simp]: "rev (rev xs) = xs"
  apply (induct xs)
   apply simp
  apply (simp add: rev_append)
  done

lemma add_zero: "x + 0 = (x::nat)"
proof -
  have "x + 0 = x" by simp
  thus ?thesis .
qed

lemma closed_by_args: "rev (rev xs) = xs"
  by (auto simp add: rev_rev_ident)

lemma trivial_dot: "x = (x::nat)" .

lift_definition ld_demo :: "nat \\<Rightarrow> nat" is "id" .

lemma no_space_before_paren: "xs = [] \\<or> length xs > 0"
proof(cases xs)
  case Nil
  show ?thesis by(simp add: Nil)
next
  case Cons
  show ?thesis by(simp add: Cons)
qed

text \u2039markup with \u2039nested\u2039\u203a\u203a cartouches and the word apply inside\u203a

end
'''


def self_test() -> int:
    """Offline checks of everything that does not need Isabelle running."""
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}"
              + (f"   {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    text = SELF_TEST_THY
    idx = LineIndex(text)
    toks = scan(text)
    cmds = probes_by_command(text, idx)
    tokens = probes_by_token(text, idx)
    cmd_names = [p.command for p in cmds]

    print("\nscanner")
    check("comment lexed as one token",
          any(t.kind == "comment" and "qed" in t.text for t in toks))
    check("nested cartouche closed correctly",
          any(t.kind == "cartouche" and t.text.endswith("\u203a") and
              "apply" in t.text for t in toks))
    check("no command found inside comment or cartouche",
          cmd_names.count("apply") == 3,
          f"got {cmd_names.count('apply')} apply commands, expected 3")
    check("by is detected mid-line", "by" in cmd_names)
    check("text terminates the preceding qed span",
          cmd_names.index("qed") < cmd_names.index("text"))

    print("\npositions")
    lines = text.splitlines()
    ok = True
    for p in cmds:
        line = lines[p.line]
        # character is a UTF-16 offset; re-derive it to compare
        if idx.encoding == "utf16":
            col = len(line.encode("utf-16-le")) // 2
        else:
            col = len(line)
        if p.character > col:
            ok = False
    check("every caret lands within its line", ok)
    check("carets are 0-indexed", cmds[0].line == 0)
    check("token mode is a superset of command mode",
          len(tokens) > len(cmds), f"{len(tokens)} vs {len(cmds)}")
    apply_probe = next(p for p in cmds if p.command == "apply")
    check("command probe captures the whole command",
          apply_probe.probe_text == "apply (induct xs)",
          repr(apply_probe.probe_text))

    print("\nsymbols")
    tbl = {"\\<And>": "\u22c0", "\\<Longrightarrow>": "\u27f9"}
    check("symbol substitution",
          decode_symbols("\\<And>x. \\<Longrightarrow>", tbl) == "\u22c0x. \u27f9")
    check("unknown symbols survive",
          decode_symbols("\\<zzz>", tbl) == "\\<zzz>")

    print("\ntransitions")
    P = dict(in_proof=True)
    rows = [
        StateRow("f.thy", "S", 7, 0, "lemma foo", "lemma", "goal:\n 1. A", **P),
        StateRow("f.thy", "S", 8, 0, "apply (induct xs)", "apply", "goal:\n 1. B", **P),
        StateRow("f.thy", "S", 9, 0, "done", "done", "", **P),
        StateRow("g.thy", "S", 3, 0, "apply auto", "apply", "goal:\n 1. C", **P),
    ]
    tr = list(build_transitions(rows))
    check("pairs are consecutive within a file", len(tr) == 2, f"got {len(tr)}")
    check("no pair spans two files",
          all(t["file"] == "f.thy" for t in tr))
    check("state_before is the previous row's state",
          tr[0]["state_before"] == "goal:\n 1. A"
          and tr[0]["tactic"] == "apply (induct xs)")

    boundary = [
        StateRow("f.thy", "S", 13, 0, "apply simp", "apply", "goal:\nNo subgoals!", **P),
        StateRow("f.thy", "S", 22, 0, 'lemma next_one: "..."', "lemma", "goal:\n 1. Z", **P),
        StateRow("f.thy", "S", 23, 0, "apply auto", "apply", "goal:\n 1. Y", **P),
    ]
    outside = [
        StateRow("f.thy", "S", 1, 0, "theory T", "theory", "x"),
        StateRow("f.thy", "S", 2, 0, "imports Main", "imports", "y"),
    ]
    check("no transition outside a proof",
          len(list(build_transitions(outside))) == 0)
    bt = list(build_transitions(boundary))
    check("no transition spans a proof boundary",
          len(bt) == 1 and bt[0]["command"] == "apply",
          f"got {[t['command'] for t in bt]}")

    print("\nproof blocks")
    blocks = proof_blocks(text)
    check("one block per lemma", len(blocks) == 6, f"got {len(blocks)}")
    ends = {e for _, e in blocks}
    offs = {p.offset for p in probes_by_command(text, idx)}
    check("every block end coincides with a caret position",
          ends <= offs, f"{len(ends - offs)} block ends unreachable")
    nospace = next(text[s:e] for s, e in blocks if "no_space_before_paren" in text[s:e])
    check("proof(cases ...) is recognised without a space",
          nospace.rstrip().endswith("qed"), repr(nospace[-24:]))
    check("by(...) inside a structured proof does not close it",
          nospace.count("by(simp") == 2, str(nospace.count("by(simp")))
    argblk = next(text[s:e] for s, e in blocks if "closed_by_args" in text[s:e])
    check("block closed by `by` includes its arguments",
          argblk.rstrip().endswith("rev_rev_ident)"), repr(argblk[-30:]))
    check("apply-script block ends at done",
          text[blocks[0][0]:blocks[0][1]].rstrip().endswith("done"))
    check("structured block ends at qed",
          text[blocks[1][0]:blocks[1][1]].rstrip().endswith("qed"))
    check("nested by does not close the outer proof",
          text[blocks[1][0]:blocks[1][1]].count("by simp") == 1)

    ctx = probes_by_command(text, idx)
    attach_context(text, ctx)
    closers = [p for p in ctx if p.in_proof and p.continuation == ""]
    check("continuation empty exactly at the proof's close",
          {p.command for p in closers} >= {"done", "qed", "by"}
          and not ({"apply", "have", "show"} & {p.command for p in closers}),
          str(sorted(p.command for p in closers)))
    check("header rows are marked outside a proof",
          all(not p.in_proof for p in ctx if p.command in
              ("theory", "imports", "begin", "end", "text")))
    first_apply = next(p for p in ctx if p.command == "apply")
    check("prefix ends at the caret",
          first_apply.prefix.rstrip().endswith("(induct xs)"),
          repr(first_apply.prefix[-30:]))
    check("continuation starts with the next command",
          first_apply.continuation.strip().startswith("apply simp"))

    print("\nroot parsing")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "Demo"
        d.mkdir()
        (d / "ROOT").write_text(
            'session Demo (AFP) = "HOL-Analysis" +\n  options [timeout = 300]\n')
        s = parse_roots(Path(td))
        check("session name parsed", "Demo" in s)
        check("parent parsed",
              s.get("Demo") and s["Demo"].parent == "HOL-Analysis",
              str(s.get("Demo")))

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--afp", default="afp/thys", help="path to afp/thys")
    ap.add_argument("--isabelle", default="isabelle",
                    help="path to the isabelle-emacs binary")
    ap.add_argument("--isabelle-home", default=None,
                    help="ISABELLE_HOME, used to decode \\<...> symbols")
    ap.add_argument("--logic", default=None,
                    help="heap to load; default is the session's ROOT parent")
    ap.add_argument("--session-dirs", nargs="*", default=None,
                    help="extra -d dirs, normally the afp/thys path")
    ap.add_argument("-o", "--isabelle-option", action="append", default=[],
                    metavar="NAME=VALUE",
                    help="extra -o option passed to vscode_server; repeatable. "
                         "Appended after the library's own defaults, so it "
                         "overrides them (e.g. -o vscode_html_output=true)")
    ap.add_argument("--out", default="dataset")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--mode", choices=("commands", "tokens"), default="commands")
    ap.add_argument("--prefix-scope", choices=("proof", "file"), default="proof",
                    help="'proof' keeps prefix from the enclosing proof's "
                         "opener and ends continuation at the proof's close, "
                         "so continuation is empty exactly when the proof is "
                         "done. 'file' uses the whole theory (much larger).")
    ap.add_argument("--offset-encoding", choices=("utf16", "codepoint"),
                    default="utf16")
    ap.add_argument("--settle", type=float, default=2.0,
                    help="max seconds to wait for dynamic output per caret")
    ap.add_argument("--quiet", type=float, default=0.35,
                    help="accept output after this long with no change")
    ap.add_argument("--load-timeout", type=float, default=180.0)
    ap.add_argument("--startup-timeout", type=int, default=180)
    ap.add_argument("--file-timeout", type=float, default=1800.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--include", default=None, help="regex filter on the path")
    ap.add_argument("--format", choices=("jsonl", "json", "both"), default="jsonl",
                    help="output serialisation (default: json arrays)")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--mock", action="store_true",
                    help="run the full pipeline with a fake Isabelle backend")
    ap.add_argument("--trace", action="store_true",
                    help="print every caret probe and the state it returned")
    ap.add_argument("--trace-lines", type=int, default=12,
                    help="how many lines of each state --trace prints")
    ap.add_argument("--dump-raw", action="store_true",
                    help="log every dynamic_output message with timings to "
                         "logs/<Theory>.dynamic_output.jsonl")
    ap.add_argument("--show-blocks", action="store_true",
                    help="print proof-block segmentation for each file and "
                         "exit (offline, no Isabelle)")
    ap.add_argument("--self-test", action="store_true",
                    help="run offline checks (no Isabelle needed) and exit")
    ap.add_argument("--probe-api", action="store_true",
                    help="dump the installed client's API and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print probe positions without starting Isabelle")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    cfg = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if cfg.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if cfg.show_blocks:
        afp = Path(cfg.afp).expanduser().resolve()
        for thy in discover(afp, cfg.limit, cfg.include):
            text = thy.read_text(encoding="utf-8", errors="replace")
            idx = LineIndex(text, cfg.offset_encoding)
            probes = probes_by_command(text, idx)
            attach_context(text, probes, cfg.prefix_scope)
            blocks = proof_blocks(text)
            ends = {e for _, e in blocks}
            starts = {s for s, _ in blocks}
            offsets = {p.offset for p in probes}
            nl = text.count
            print(f"\n=== {thy}  ({len(blocks)} blocks, {len(probes)} probes)")
            for s, e in blocks:
                ls = text[:s].count("\n") + 1
                le = text[:e].count("\n") + 1
                hit = "END-PROBED" if e in offsets else "END NOT PROBED  <<<"
                print(f"  L{ls:<5}-L{le:<5} {e - s:>6} chars  {hit}")
                head = " ".join(text[s:s + 120].split())[:58]
                tail = " ".join(text[max(s, e - 120):e].split())[-58:]
                print(f"      opens: {head!r}")
                print(f"      closes:{tail!r}")
                if e not in offsets:
                    nxt = [(cs, ce) for _, cs, ce in command_spans(text)
                           if cs >= e][:1]
                    near = min((abs(p.offset - e), p.offset, p.command)
                               for p in probes) if probes else None
                    print(f"      end_off={e} nearest_probe={near}")
                    if nxt:
                        print(f"      next_cmd_at={nxt[0][0]} "
                              f"{text[nxt[0][0]:nxt[0][1]][:40]!r}")
            outside = [p for p in probes if not p.in_proof]
            print(f"  probes outside any block: {len(outside)}")
            for p in outside[:8]:
                print(f"      L{p.line+1:<5} [{p.command}] {p.probe_text[:50]}")
            fin = [p for p in probes if p.in_proof and p.continuation == ""]
            print(f"  probes at a block end: {len(fin)}")
        return 0
    if cfg.self_test:
        return self_test()
    if cfg.probe_api:
        probe_api()
        return 0
    if cfg.dry_run:
        afp = Path(cfg.afp).expanduser().resolve()
        for thy in discover(afp, cfg.limit, cfg.include):
            text = thy.read_text(encoding="utf-8", errors="replace")
            idx = LineIndex(text, cfg.offset_encoding)
            ps = (probes_by_command(text, idx) if cfg.mode == "commands"
                  else probes_by_token(text, idx))
            print(f"\n== {thy}  ({len(ps)} probes, mode={cfg.mode})")
            for p in ps[:40]:
                print(f"  {p.line+1:>5}:{p.character:<4} [{p.command}] {p.probe_text[:70]}")
        return 0
    return asyncio.run(main_async(cfg))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
