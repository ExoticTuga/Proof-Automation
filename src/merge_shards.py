#!/usr/bin/env python3
"""
merge_shards.py -- combine sharded harvest output into one dataset.

    python merge_shards.py data/train --out data/train_merged

Concatenates every shard's states.jsonl and transitions.jsonl, and checks the
things that actually go wrong when a job array is involved:

  * duplicate records, if a shard was rerun with a different shard count and
    so covered files belonging to another shard
  * theory files missing entirely, i.e. a shard that died before finishing
  * test-set leakage, if --test-entries is given

Duplicates are dropped on (file, line, character); the merge is idempotent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path):
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                print(f"  ! {path}:{n} unparseable, skipped", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("shard_root", type=Path,
                    help="directory containing shard_*/ subdirectories")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--test-entries", type=Path, default=None,
                    help="held-out entry list; merge aborts if any appears")
    ap.add_argument("--expect-entries", type=Path, default=None,
                    help="training entry list, to report coverage")
    a = ap.parse_args()

    shards = sorted(a.shard_root.glob("shard_*"))
    if not shards:
        print(f"no shard_* directories under {a.shard_root}")
        return 2
    print(f"{len(shards)} shards")

    a.out.mkdir(parents=True, exist_ok=True)
    totals = Counter()

    for name in ("states", "transitions"):
        seen: set[tuple] = set()
        dupes = 0
        out_path = a.out / f"{name}.jsonl"
        with out_path.open("w", encoding="utf-8") as out:
            for sd in shards:
                n = 0
                for rec in read_jsonl(sd / f"{name}.jsonl"):
                    key = (rec.get("file"), rec.get("line"),
                           rec.get("character"), rec.get("tactic"))
                    if key in seen:
                        dupes += 1
                        continue
                    seen.add(key)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    n += 1
                totals[sd.name] += n
        print(f"  {name}.jsonl: {len(seen)} records"
              + (f", {dupes} duplicates dropped" if dupes else ""))

    states = list(read_jsonl(a.out / "states.jsonl"))
    files = {r["file"] for r in states}
    entries = {Path(r["file"]).parent.name for r in states}
    print(f"\n{len(states)} states from {len(files)} theory files, "
          f"{len(entries)} entries")

    empty = [s.name for s in shards
             if not (s / "states.jsonl").is_file()
             or (s / "states.jsonl").stat().st_size == 0]
    if empty:
        print(f"\n!! {len(empty)} shards produced nothing: {empty[:8]}")
        print("   check slurm_logs/ for those array indices before using this data")

    if a.expect_entries:
        want = {ln.strip() for ln in a.expect_entries.read_text().splitlines()
                if ln.strip()}
        missing = want - entries
        print(f"\ncoverage: {len(entries)}/{len(want)} entries "
              f"({100.0 * len(entries) / max(1, len(want)):.1f}%)")
        if missing:
            print(f"  {len(missing)} entries yielded no states, e.g. "
                  f"{sorted(missing)[:5]}")
            print("  (usually an unbuilt parent heap or a load failure)")

    if a.test_entries:
        held = {ln.strip() for ln in a.test_entries.read_text().splitlines()
                if ln.strip()}
        leaked = entries & held
        if leaked:
            print(f"\n!! LEAKAGE: {len(leaked)} held-out entries in the "
                  f"training data: {sorted(leaked)[:5]}")
            print("   do not train on this merge; fix the entry list first")
            return 1
        print(f"\nno leakage: 0 of {len(held)} held-out entries present")

    print(f"\nwrote {a.out}/states.jsonl and {a.out}/transitions.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
