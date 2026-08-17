#!/bin/bash
#SBATCH --job-name=afp-harvest
#SBATCH --partition=cpu
#SBATCH --array=0-31%16
#SBATCH --cpus-per-task=2
#SBATCH --mem=6G
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
#   stdout. So cores are nearly free and MEMORY is the binding constraint --
#   each Isabelle process with the HOL heap wants ~4 GB, and cpu nodes have
#   ~15.4 GB, giving 3-4 concurrent tasks per node.
#
#   --array=0-31%16 runs 32 shards with at most 16 at once. Raise the %N once
#   you have seen how many nodes the partition actually gives you; lower it if
#   tasks are being OOM-killed.
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
ISABELLE="$HOME/isabelle-emacs/bin/isabelle"
ENTRIES="$PROJECT/split_hol/train_entries.txt"
OUTROOT="$PROJECT/data/train"
VENV="$PROJECT/.venv"
SETTLE=1.0
# --------------------------------------------------------------------------

NUM_SHARDS=${SLURM_ARRAY_TASK_COUNT:-32}
SHARD=${SLURM_ARRAY_TASK_ID:-0}

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
