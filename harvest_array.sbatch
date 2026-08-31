#!/bin/bash
#SBATCH --job-name=afp-harvest
#SBATCH --partition=cpu
#SBATCH --array=0-31%14
#SBATCH --cpus-per-task=2
#SBATCH --mem=24G
#SBATCH --time=24:00:00
#SBATCH --output=slurm_logs/harvest_%A_%a.out
#SBATCH --error=slurm_logs/harvest_%A_%a.err

# ---------------------------------------------------------------------------
# Parallel AFP harvest as a Slurm job array.
#
#   sbatch harvest_array.sbatch
#
# WHY THESE RESOURCES
#   The workload is latency-bound, not CPU-bound: measured 28 min wall against
#   20 s of CPU (~6% utilisation) because the process blocks on Isabelle's
#   stdout. So cores are nearly free and MEMORY is the binding constraint.
#
#   MEASURED: 6 GB was OOM-killed on nearly every shard; at 12 GB, 10 of 14
#   completed with peaks from 5.1 to 12.0 GB -- marginal. 24 GB is the working
#   figure. Memory demand is driven by individual theory complexity, not by
#   how many files a shard holds.
#   Note that `sinfo` reports the SMALLEST node in a partition ("15788+"):
#   cpu02 actually has 192 GB, so 14 x 12 GB fits comfortably.
#
#   --array=0-31%14 runs 32 shards with at most 14 at once.
#
# RESTARTS
#   Every task passes --resume, so re-running the same sbatch skips theory
#   files already in that shard's checkpoint. A killed or timed-out task is
#   recovered by resubmitting; nothing is recomputed.
# ---------------------------------------------------------------------------

set -euo pipefail

# --- configure ------------------------------------------------------------
PROJECT="$HOME/Proof-Automation"
AFP="$PROJECT/src/afp-2026-06-29/thys"
ISABELLE="$HOME/isabelle-emacs/bin/isabelle"   # verified present
ENTRIES="$PROJECT/split_hol/test_entries.txt"
OUTROOT="$PROJECT/data/test"
VENV="$PROJECT/.venv"
SETTLE=1.0
# --------------------------------------------------------------------------

# NUM_SHARDS must be FIXED, not derived from SLURM_ARRAY_TASK_COUNT: when you
# resubmit a subset (e.g. --array=0,2,14), TASK_COUNT is the size of that
# subset while TASK_IDs keep their original values, so shard 14 of 3 is
# rejected -- and worse, shards that happen to fall inside the smaller range
# silently re-shard against the wrong divisor.
NUM_SHARDS=32
SHARD=${SLURM_ARRAY_TASK_ID:-0}

if [ "$SHARD" -ge "$NUM_SHARDS" ]; then
  echo "shard $SHARD >= NUM_SHARDS $NUM_SHARDS -- raise NUM_SHARDS" >&2
  exit 1
fi

mkdir -p slurm_logs "$OUTROOT" "$PROJECT/data/logs"

# Isabelle needs a writable home; on a cluster keep it off the home quota and
# give each task its own so concurrent tasks cannot corrupt each other's state.
export ISABELLE_HOME_USER="${SLURM_TMPDIR:-/tmp}/isabelle_${SLURM_JOB_ID:-0}_${SHARD}"
mkdir -p "$ISABELLE_HOME_USER"
trap 'rm -rf "$ISABELLE_HOME_USER"' EXIT

source "$VENV/bin/activate"
cd "$PROJECT/src"

echo "=== shard $SHARD of $NUM_SHARDS on $(hostname) at $(date)"
echo "=== ISABELLE_HOME_USER=$ISABELLE_HOME_USER"

python afp_harvest.py \
  --afp "$AFP" \
  --entries "$ENTRIES" \
  --shard "$SHARD" --num-shards "$NUM_SHARDS" \
  --isabelle "$ISABELLE" \
  --session-dirs "$AFP" \
  --logic HOL \
  --settle "$SETTLE" \
  --out "$OUTROOT/shard_$SHARD" \
  --log-dir "$PROJECT/data/logs/shard_$SHARD" \
  --resume -v

echo "=== shard $SHARD finished at $(date)"
wc -l "$OUTROOT/shard_$SHARD/states.jsonl" || true
