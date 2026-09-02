#!/usr/bin/env python3
"""
compare_completion.py -- check two proof-completion runs are comparable.

    python compare_completion.py results/completion_final.jsonl \
                                 results/completion_base.jsonl

Reports which theorems each run attempted, which appear in only one, and the
per-theorem outcomes side by side. A completion figure means nothing unless
both models faced the same theorems, so this is checked explicitly rather than
assumed from a shared random seed.
"""
import json, sys
from pathlib import Path

def load(p):
    return {(r["file"], r["line"]): r
            for r in (json.loads(l) for l in open(p) if l.strip())}

a_path, b_path = Path(sys.argv[1]), Path(sys.argv[2])
A, B = load(a_path), load(b_path)

print(f"A = {a_path.name}: {len(A)} attempts, {sum(r['proved'] for r in A.values())} proved")
print(f"B = {b_path.name}: {len(B)} attempts, {sum(r['proved'] for r in B.values())} proved")

only_a, only_b = set(A) - set(B), set(B) - set(A)
both = set(A) & set(B)
print(f"\ncommon: {len(both)}   only in A: {len(only_a)}   only in B: {len(only_b)}")

for label, keys, src in (("only in A", only_a, A), ("only in B", only_b, B)):
    for k in sorted(keys):
        print(f"  {label}: {Path(k[0]).name}:{k[1]}  ({src[k]['reason'][:60]})")

if only_a or only_b:
    print("\n!! The runs are NOT like-for-like. Restrict to the common set,")
    print("   or rerun with --targets-in so both attempt an identical list.")

pa = sum(A[k]["proved"] for k in both)
pb = sum(B[k]["proved"] for k in both)
print(f"\nON THE COMMON {len(both)} THEOREMS")
print(f"  A: {pa}/{len(both)} = {pa/max(1,len(both)):.4f}")
print(f"  B: {pb}/{len(both)} = {pb/max(1,len(both)):.4f}")

print(f"\n{'theorem':<42}{'A':>10}{'B':>10}")
for k in sorted(both):
    ra, rb = A[k], B[k]
    name = f"{Path(k[0]).name}:{k[1]}"
    print(f"{name[:41]:<42}{'PROVED' if ra['proved'] else '-':>10}"
          f"{'PROVED' if rb['proved'] else '-':>10}")

from collections import Counter
print("\nfailure reasons, A:")
for r, n in Counter(A[k]["reason"].split(":")[0] for k in both
                    if not A[k]["proved"]).most_common():
    print(f"  {n:>3}  {r}")
print("failure reasons, B:")
for r, n in Counter(B[k]["reason"].split(":")[0] for k in both
                    if not B[k]["proved"]).most_common():
    print(f"  {n:>3}  {r}")
