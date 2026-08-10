#!/usr/bin/env python3
"""
probe_state.py -- verify the PIDE state panel channel.

    python probe_state.py <isabelle_exec> <theory.thy> [logic]

Does exactly three things:
  1. sends the PIDE/state_init REQUEST and reads back the state_id
  2. registers a listener for PIDE/state_output notifications
  3. moves the caret to a few positions and prints what arrives

The state panel is separate from PIDE/dynamic_output: dynamic_output is the
*output* panel (solve_direct hints, the finished theorem), while state_output
is the *state* panel ("proof (prove) / goal (N subgoals)"). The panel sets
auto_update_enabled = true at construction and refreshes on Session.Caret_Focus,
so no explicit state_update poke is needed after each move.
"""

import asyncio
import logging
import sys
from pathlib import Path

from panel_text import panel_html_to_text

logging.basicConfig(level=logging.WARNING,
                    format="%(levelname)-7s %(message)s")

STATE_INIT = "PIDE/state_init"
STATE_OUTPUT = "PIDE/state_output"
STATE_UPDATE = "PIDE/state_update"


class RawRequest:
    """Minimal stand-in for lsp_client's BaseRequest.

    LSPClient.request() only sets `.id` and calls
    `.model_dump(exclude_none=True)`, so nothing more is needed and we avoid
    depending on the library's pydantic model layout.
    """

    def __init__(self, method: str, params=None) -> None:
        self.jsonrpc = "2.0"
        self.id = None
        self.method = method
        self.params = params

    def model_dump(self, exclude_none: bool = True) -> dict:
        d = {"jsonrpc": self.jsonrpc, "id": self.id,
             "method": self.method, "params": self.params}
        return {k: v for k, v in d.items()
                if not (exclude_none and v is None)}


def get_lsp_client(document):
    """document -> IsabelleClient -> LSPClient, whatever it's called."""
    isabelle = getattr(document, "isabelle", None)
    if isabelle is None:
        raise RuntimeError("document has no .isabelle")
    for name in ("lspClient", "lsp_client", "client", "_lspClient"):
        c = getattr(isabelle, name, None)
        if c is not None and hasattr(c, "request"):
            print(f"[ok] found LSPClient at IsabelleClient.{name}")
            return c
    raise RuntimeError(
        "no LSPClient on IsabelleClient; attributes are: "
        + ", ".join(a for a in dir(isabelle) if not a.startswith("__")))


class Probe:
    def __init__(self, positions):
        self.positions = positions
        self.state_id = None
        self.messages = []
        self.done = asyncio.Event()
        self.error = None

    async def on_state_output(self, document, response, timestamp):
        params = response.get("params") or {}
        self.messages.append(params)
        content = params.get("content", "")
        text = panel_html_to_text(content)
        caret = getattr(document, "caret_position", None)
        print(f"\n>>> state_output  id={params.get('id')} "
              f"auto_update={params.get('auto_update')} caret={caret} "
              f"({len(content)} chars html -> {len(text)} chars text)")
        for line in text.splitlines()[:10] or ["<empty>"]:
            print("    " + line)

    async def on_dynamic_output(self, document, response, timestamp):
        content = (response.get("params") or {}).get("content", "")
        text = panel_html_to_text(content)
        first = (text.splitlines() or ["<empty>"])[0]
        print(f"    (dynamic_output: {first[:70]})")

    async def on_start(self, document, **kwargs):
        try:
            lsp = get_lsp_client(document)

            print(f"[..] sending {STATE_INIT}")
            result = await lsp.request(RawRequest(STATE_INIT), timeout=30)
            print(f"[ok] state_init result: {result!r}")
            self.state_id = (result or {}).get("state_id")
            if self.state_id is None:
                raise RuntimeError(f"no state_id in reply: {result!r}")

            for (line, char) in self.positions:
                print(f"\n[..] caret -> line {line + 1}, char {char}")
                await document.move_caret(line, char)
                await asyncio.sleep(2.0)

            print(f"\n[--] {len(self.messages)} state_output messages total")
        except BaseException as e:                       # noqa: BLE001
            self.error = e
            print(f"[!!] {type(e).__name__}: {e}")
        finally:
            self.done.set()


async def main(exec_path: str, theory: str, logic: str) -> int:
    from isabelle_lsp_client import ClientHandler, IsabelleProcess

    thy = Path(theory).resolve()
    text = thy.read_text(encoding="utf-8", errors="replace").splitlines()

    # probe the end of every line that starts with a proof command
    positions = []
    for i, line in enumerate(text):
        head = line.strip().split(" ")[0] if line.strip() else ""
        if head in ("lemma", "apply", "by", "done", "proof", "qed", "have"):
            positions.append((i, len(line.rstrip())))
    positions = positions[:8]
    print(f"probing {len(positions)} positions in {thy.name}")

    probe = Probe(positions)
    handler = ClientHandler()
    handler.register_on_start(probe.on_start)
    handler.register(STATE_OUTPUT, probe.on_state_output)
    handler.register_on_dynamic_output(probe.on_dynamic_output)

    args = {
        "exec": exec_path,
        "options": ["-l", logic, "-o", "vscode_html_output=true"],
        "log_path": "/tmp/probe_state.log",
        "theory": str(thy),
        "startup_timeout": 180,
    }

    process = IsabelleProcess(handler)
    run_task = asyncio.create_task(process.run(args))
    done_task = asyncio.create_task(probe.done.wait())
    await asyncio.wait({run_task, done_task},
                       return_when=asyncio.FIRST_COMPLETED)
    if run_task.done() and not probe.done.is_set():
        exc = run_task.exception()
        if exc:
            raise exc
    for t in (run_task, done_task):
        if not t.done():
            t.cancel()
    await asyncio.gather(run_task, done_task, return_exceptions=True)
    return 1 if probe.error else 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    logic = sys.argv[3] if len(sys.argv) > 3 else "HOL"
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2], logic)))
