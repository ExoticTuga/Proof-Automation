#!/bin/bash
#SBATCH --job-name=qwen-eval
#SBATCH --partition=gpu.stu
#SBATCH --gres=gpu:ampere:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=08:00:00
#SBATCH --output=slurm_logs/eval_%j.out
#SBATCH --error=slurm_logs/eval_%j.err

# ---------------------------------------------------------------------------
# Generate next-tactic predictions and score them against held-out references.
#
#   MODEL=runs/qwen7b/final \
#   DATA=data/sft_test/train.jsonl \
#   OUTFILE=results/test_final.jsonl \
#   LIMIT=10000 sbatch eval.sbatch
#
# All four are optional; the defaults below evaluate the final model on the
# held-out test partition.
#
# ONE GPU, NOT SEVERAL
#   The script runs a single process. Spreading a 15 GB model over two cards
#   would shuttle activations between them rather than doubling throughput,
#   and it fits on one 80 GB A100 with room to spare. Increase --batch-size
#   for speed instead. For the full 70 k set, run several single-GPU jobs over
#   disjoint slices and concatenate.
#
# LIMIT
#   10 000 examples give a margin of error under +/-1% on a proportion near
#   0.34, which is well inside the difference between any two checkpoints
#   worth distinguishing. Set LIMIT=0 to score the whole file.
# ---------------------------------------------------------------------------

set -uo pipefail
PROJECT="$HOME/Proof-Automation"
source "$PROJECT/.venv/bin/activate"
cd "$PROJECT"
export HF_HOME="$PROJECT/.hf"
export HF_HUB_OFFLINE=1
export TOKENIZERS_PARALLELISM=false

MODEL="${MODEL:-runs/qwen7b/final}"
DATA="${DATA:-data/sft_test/train.jsonl}"
OUTFILE="${OUTFILE:-results/test_final.jsonl}"
LIMIT="${LIMIT:-10000}"

mkdir -p results
echo "=== $(hostname) $(date)"
echo "=== model=$MODEL data=$DATA limit=$LIMIT"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

LIMIT_ARG=""
[ "$LIMIT" != "0" ] && LIMIT_ARG="--limit $LIMIT"

python -u src/eval_generate.py \
  --model "$MODEL" \
  --data "$DATA" \
  --out "$OUTFILE" \
  --batch-size 32 \
  $LIMIT_ARG

echo "=== finished $(date)"
