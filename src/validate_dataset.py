#!/usr/bin/env python3
"""
validate_dataset.py -- sanity-check the output of afp_harvest.py.

    python validate_dataset.py dataset/            # full report
    python validate_dataset.py dataset/ --sample 5 # print 5 random rows

The most important check is ALIGNMENT: it re-reads each source .thy file and
verifies that the text ending at the recorded (line, character) really is the
command recorded in `probe`. If that fails, your caret was landing somewhere
other than where you think, and every state in the dataset is attributed to the
wrong command -- which is silent and unrecoverable if you don't check for it.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

ERROR_MARKERS = (
    "Failed to", "Undefined", "Type unification failed", "Inner syntax error",
    "Outer syntax error", "Malformed", "Illegal application",
    "*** ", "exception", "Timeout",
)
STATE_PREFIXES = ("proof (", "goal", "theorem", "No subgoals", "using this",
                  "this:", "constants", "type")
# a leftover '<' means the HTML panel output was not converted to text
HTML_MARKERS = ("<pre ", "<span ", "&amp;", "&lt;")


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(l) for l in text.splitlines() if l.strip()]
    return json.loads(text)


def char_to_offset(line_text: str, character: int, encoding: str) -> int:
    """Invert a UTF-16 (or codepoint) column back to a Python string index."""
    if encoding == "codepoint":
        return min(character, len(line_text))
    units = 0
    for i, ch in enumerate(line_text):
        if units >= character:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(line_text)


def check_alignment(states: list[dict], encoding: str) -> tuple[int, int, list[str]]:
    """Does the source text at (line, character) end with the recorded probe?"""
    cache: dict[str, list[str]] = {}
    ok = bad = 0
    examples: list[str] = []
    for r in states:
        f = r["file"]
        if f not in cache:
            try:
                cache[f] = Path(f).read_text(encoding="utf-8",
                                             errors="replace").splitlines()
            except OSError:
                cache[f] = []
        lines = cache[f]
        i = r["line"] - 1
        if not lines or i >= len(lines):
            bad += 1
            continue
        line_text = lines[i]
        off = char_to_offset(line_text, r["character"], encoding)
        prefix = line_text[:off]
        # the caret should sit immediately after the last token of the probe
        tail = r["probe"].split()[-1] if r["probe"].split() else ""
        if tail and prefix.rstrip().endswith(tail):
            ok += 1
        else:
            bad += 1
            if len(examples) < 5:
                examples.append(
                    f"{Path(f).name}:{r['line']}:{r['character']} "
                    f"probe={r['probe'][:40]!r} but line ends {prefix[-40:]!r}")
    return ok, bad, examples


def report(out_dir: Path, encoding: str, sample: int) -> int:
    states = load(out_dir / "states.jsonl") or load(out_dir / "states.json")
    trans = load(out_dir / "transitions.jsonl") or load(out_dir / "transitions.json")
    if not states:
        print(f"no states found in {out_dir}")
        return 2

    files = {r["file"] for r in states}
    print(f"{len(states)} states, {len(trans)} transitions, {len(files)} files\n")

    print("== alignment (caret vs source) ==")
    ok, bad, examples = check_alignment(states, encoding)
    pct = 100.0 * ok / max(1, ok + bad)
    print(f"  {ok} aligned, {bad} misaligned ({pct:.1f}% ok)")
    for e in examples:
        print("   !", e)
    if pct < 95:
        print("   >> positions are wrong. Try --offset-encoding codepoint,")
        print("      or check that caret_update is 0-indexed in your client.")

    print("\n== state quality ==")
    empty = sum(1 for r in states if not r["state"].strip())
    errs = [r for r in states
            if any(m in r["state"] or m in r.get("output", "")
                   for m in ERROR_MARKERS)]
    odd = [r for r in states
           if r["state"].strip()
           and not r["state"].lstrip().startswith(STATE_PREFIXES)]
    print(f"  empty states:        {empty}")
    print(f"  states with errors:  {len(errs)}")
    html = [r for r in states if any(m in r["state"] for m in HTML_MARKERS)]
    print(f"  unrecognised shape:  {len(odd)}")
    print(f"  un-stripped HTML:    {len(html)}")
    for r in errs[:3]:
        print(f"   ! {Path(r['file']).name}:{r['line']} {r['state'].splitlines()[0][:70]}")
    for r in odd[:3]:
        print(f"   ? {Path(r['file']).name}:{r['line']} {r['state'].splitlines()[0][:70]}")

    print("\n== coverage ==")
    per_file = Counter(r["file"] for r in states)
    thin = [f for f, n in per_file.items() if n < 3]
    print(f"  states per file: min={min(per_file.values())} "
          f"median={sorted(per_file.values())[len(per_file)//2]} "
          f"max={max(per_file.values())}")
    print(f"  files with <3 states: {len(thin)}")
    for f in thin[:5]:
        print(f"   ! {Path(f).name} ({per_file[f]}) -- probably failed to load")

    print("\n== commands ==")
    for cmd, n in Counter(r["command"] for r in states).most_common(12):
        print(f"  {cmd:<16} {n}")

    if trans:
        print("\n== transitions ==")
        tac = Counter(t["command"] for t in trans)
        for cmd, n in tac.most_common(10):
            print(f"  {cmd:<16} {n}")
        dupes = Counter((t["state_before"], t["tactic"]) for t in trans)
        n_dupe = sum(v - 1 for v in dupes.values() if v > 1)
        print(f"  duplicate (state_before, tactic) pairs: {n_dupe}")
        closing = Counter(t["command"] for t in trans
                          if t["command"] in ("by", "done", "qed"))
        total_closing = sum(closing.values())
        detail = ", ".join(f"{k}={v}" for k, v in closing.most_common())
        print(f"  proof-closing steps: {total_closing} "
              f"({100.0*total_closing/len(trans):.0f}% of rows)  [{detail}]")
        print(f"  final steps of a proof (remaining<=1): "
              f"{sum(1 for t in trans if t.get('last_step'))}")

    print("\n== training context ==")
    with_prefix = sum(1 for r in states if r.get("prefix"))
    in_proof = sum(1 for r in states if r.get("in_proof"))
    outside = [r for r in states if not r.get("in_proof")]
    true_stop = [r for r in states
                 if r.get("in_proof") and r.get("continuation") == ""]
    print(f"  rows with a prefix:   {with_prefix}/{len(states)}")
    print(f"  rows inside a proof:  {in_proof}/{len(states)}")
    print(f"  outside any proof:    {len(outside)}  (filter before training)")
    print(f"  proof-finished rows:  {len(true_stop)}  (empty continuation)")
    last = [r for r in states if r.get("remaining") == 1]
    zero = [r for r in states if r.get("remaining") == 0]
    print(f"  last step of a proof: {len(last)}  (remaining==1: only the "
          f"closing command left)")
    print(f"  after the close:      {len(zero)}  (remaining==0; usually 0 "
          f"because Isabelle emits no state there)")
    if with_prefix:
        lens = sorted(len(r.get("prefix", "")) for r in states if r.get("prefix"))
        print(f"  prefix chars: min={lens[0]} median={lens[len(lens)//2]} "
              f"max={lens[-1]}")
    bad = [r for r in states
           if r.get("in_proof") and r.get("prefix")
           and not r["prefix"].rstrip().endswith(
               (r["probe"].split()[-1] if r["probe"].split() else "\0"))]
    print(f"  prefix not ending at the probe: {len(bad)}")
    proofy = {"apply", "by", "done", "qed", "show", "have", "proof", "next",
              "case", "thus", "hence", "unfolding", "using", "obtain"}
    orphan = [r for r in states
              if r["command"] in proofy and not r.get("in_proof")]
    print(f"  proof commands marked outside a proof: {len(orphan)}")
    for r in orphan[:5]:
        print(f"   ! {Path(r['file']).name}:{r['line']} [{r['command']}] "
              f"{r['probe'][:50]}")
    if orphan:
        print("   >> proof-block detection is disagreeing with the command "
              "scanner; these rows lose their prefix and continuation.")

    print("\n== contamination ==")
    incomplete = []
    for f in files:
        try:
            body = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"\b(sorry|oops)\b", body):
            incomplete.append(f)
    print(f"  files containing sorry/oops: {len(incomplete)}")
    for f in incomplete[:5]:
        print(f"   ! {Path(f).name}")

    if sample:
        print("\n== sample rows ==")
        for t in random.sample(trans or states, min(sample, len(trans or states))):
            print("-" * 60)
            if "state_before" in t:
                print(f"{Path(t['file']).name}:{t['line']}")
                print("  BEFORE:")
                for ln in t["state_before"].splitlines()[:8]:
                    print("    " + ln)
                print(f"  TACTIC: {t['tactic']}")
                print("  AFTER:")
                for ln in t["state_after"].splitlines()[:8]:
                    print("    " + ln)
            else:
                print(f"{Path(t['file']).name}:{t['line']}  [{t['command']}] {t['probe']}")
                for ln in t["state"].splitlines()[:8]:
                    print("    " + ln)

    return 1 if (pct < 95 or empty > len(states) * 0.2) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", type=Path, default=Path("dataset"), nargs="?")
    ap.add_argument("--offset-encoding", choices=("utf16", "codepoint"),
                    default="utf16")
    ap.add_argument("--sample", type=int, default=0)
    a = ap.parse_args()
    return report(a.out_dir, a.offset_encoding, a.sample)


if __name__ == "__main__":
    sys.exit(main())
