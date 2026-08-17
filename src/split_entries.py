#!/usr/bin/env python3
"""
split_entries.py -- stratified train/test split over AFP entries.

    python split_entries.py --afp afp-2026-06-29/thys --test-frac 0.2

Writes train_entries.txt and test_entries.txt (one entry name per line), plus
split_report.txt showing the stratification.

WHY ENTRIES, NOT PROOFS
-----------------------
Holding out individual proofs from inside an entry leaks: the model sees the
rest of that theory in training -- its definitions, notation, lemma library and
proof idioms -- so a held-out proof from the same file is not really unseen and
the evaluation number is optimistic. Splitting whole entries means test proofs
come from developments the model has never encountered.

STRATIFICATION
--------------
A positional split (e.g. the last 20% of entries alphabetically) would bias the
test set toward whatever subject areas sort last. Entries are therefore grouped
into strata and the test fraction is sampled from within each, so every area is
represented on both sides.

Strata are derived from, in order of preference:
  1. an AFP metadata file, if one ships with the snapshot (topic per entry)
  2. the parent session declared in the entry's ROOT (HOL, HOL-Analysis,
     HOL-Probability, ...), which correlates strongly with subject area:
     analysis entries import HOL-Analysis, probability entries HOL-Probability,
     and so on
Within a stratum, entries are also balanced by size so the test set is not
composed entirely of small or large developments.
"""

from __future__ import annotations

import argparse
import configparser
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

SESSION_RE = re.compile(
    r'^\s*session\s+"?([\w.\-]+)"?\s*(?:\([^)]*\))?\s*(?:=\s*"?([\w.\-]+)"?\s*\+)?',
    re.MULTILINE,
)


def find_metadata(afp_root: Path) -> dict[str, str]:
    """Entry -> topic, if the snapshot ships AFP metadata. Empty if not."""
    candidates = [
        afp_root / "metadata" / "metadata",
        afp_root.parent / "metadata" / "metadata",
        afp_root.parent / "etc" / "metadata",
        afp_root.parent / "metadata",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            cp = configparser.ConfigParser(strict=False, interpolation=None)
            cp.read(path, encoding="utf-8")
        except Exception:
            continue
        topics: dict[str, str] = {}
        for entry in cp.sections():
            raw = cp[entry].get("topics", "").strip()
            if raw:
                first = raw.splitlines()[0].strip()
                topics[entry] = first.split("/")[0].strip() or "unknown"
        if topics:
            print(f"[ok] topics from {path} ({len(topics)} entries)")
            return topics
    print("[--] no AFP metadata found; using ROOT parent session as the stratum")
    return {}


def entry_info(afp_root: Path) -> list[dict]:
    out = []
    for d in sorted(p for p in afp_root.iterdir() if p.is_dir()):
        thys = sorted(d.glob("*.thy"))
        if not thys:
            continue
        parent = "unknown"
        root = d / "ROOT"
        if root.is_file():
            body = root.read_text(encoding="utf-8", errors="replace")
            m = SESSION_RE.findall(body)
            if m:
                parent = m[0][1] or "HOL"
        out.append({
            "name": d.name,
            "parent": parent,
            "n_thy": len(thys),
            "size": sum(t.stat().st_size for t in thys),
        })
    return out


def split(entries: list[dict], topics: dict[str, str], frac: float,
          seed: int) -> tuple[list[dict], list[dict]]:
    strata: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        e["stratum"] = topics.get(e["name"], e["parent"])
        strata[e["stratum"]].append(e)

    rng = random.Random(seed)
    train: list[dict] = []
    test: list[dict] = []

    for name in sorted(strata):
        group = sorted(strata[name], key=lambda e: e["size"])
        # interleave by size so the test set is not all small or all large
        k = max(1, round(len(group) * frac)) if len(group) >= 3 else 0
        picked = set()
        if k:
            # take every len/k-th entry along the size ordering, jittered by seed
            step = len(group) / k
            offset = rng.random() * step
            for i in range(k):
                idx = min(len(group) - 1, int(offset + i * step))
                while idx in picked and idx + 1 < len(group):
                    idx += 1
                picked.add(idx)
        for i, e in enumerate(group):
            (test if i in picked else train).append(e)
    return train, test


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--afp", required=True, help="path to afp-*/thys")
    ap.add_argument("--test-frac", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=20260815)
    ap.add_argument("--out-dir", type=Path, default=Path("."))
    ap.add_argument("--only-parents", nargs="*", default=None,
                    help="restrict to entries with these ROOT parents, e.g. HOL")
    a = ap.parse_args()

    afp = Path(a.afp).expanduser().resolve()
    if not afp.is_dir():
        print(f"not a directory: {afp}")
        return 2

    entries = entry_info(afp)
    if a.only_parents:
        entries = [e for e in entries if e["parent"] in a.only_parents]
    topics = find_metadata(afp)
    train, test = split(entries, topics, a.test_frac, a.seed)

    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "train_entries.txt").write_text(
        "\n".join(e["name"] for e in sorted(train, key=lambda x: x["name"])) + "\n")
    (a.out_dir / "test_entries.txt").write_text(
        "\n".join(e["name"] for e in sorted(test, key=lambda x: x["name"])) + "\n")

    lines = [
        f"seed={a.seed}  test_frac={a.test_frac}",
        f"entries: {len(entries)}  train: {len(train)}  test: {len(test)} "
        f"({100.0 * len(test) / max(1, len(entries)):.1f}%)",
        f"theory files: train {sum(e['n_thy'] for e in train)}  "
        f"test {sum(e['n_thy'] for e in test)}",
        f"bytes: train {sum(e['size'] for e in train) / 1e6:.1f} MB  "
        f"test {sum(e['size'] for e in test) / 1e6:.1f} MB",
        "",
        f"{'stratum':<34}{'train':>7}{'test':>7}{'test%':>8}",
    ]
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in train:
        by[e["stratum"]][0] += 1
    for e in test:
        by[e["stratum"]][1] += 1
    for s in sorted(by, key=lambda k: -(by[k][0] + by[k][1])):
        tr, te = by[s]
        pct = 100.0 * te / max(1, tr + te)
        lines.append(f"{s[:33]:<34}{tr:>7}{te:>7}{pct:>7.0f}%")

    report = "\n".join(lines)
    (a.out_dir / "split_report.txt").write_text(report + "\n")
    print("\n" + report)
    print(f"\nwrote train_entries.txt ({len(train)}), "
          f"test_entries.txt ({len(test)}), split_report.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
