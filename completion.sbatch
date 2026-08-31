#!/bin/bash
#SBATCH --job-name=proof-completion
#SBATCH --partition=gpu.stu
#SBATCH --gres=gpu:ampere:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=16:00:00
#SBATCH --output=slurm_logs/completion_%j.out
#SBATCH --error=slurm_logs/completion_%j.err

# ---------------------------------------------------------------------------
# Proof completion: the model regenerates whole proofs and Isabelle judges
# them. Needs BOTH a GPU (generation) and CPU (the Isabelle process), so it
# runs on a GPU node with several cores.
#
#   MODEL=runs/qwen7b/final LIMIT=100 sbatch completion.sbatch
#
# Run it twice -- once on the fine-tuned model, once on the base model -- to
# establish whether fine-tuning helped on the metric that matters:
#
#   MODEL=Qwen/Qwen2.5-Coder-7B OUTFILE=results/completion_base.jsonl \
#     sbatch completion.sbatch
# ---------------------------------------------------------------------------

set -uo pipefail
PROJECT="$HOME/Proof-Automation"
source "$PROJECT/.venv/bin/activate"
cd "$PROJECT"
export HF_HOME="$PROJECT/.hf"
export HF_HUB_OFFLINE=1
export TOKENIZERS_PARALLELISM=false

MODEL="${MODEL:-runs/qwen7b/final}"
OUTFILE="${OUTFILE:-results/completion_final.jsonl}"
LIMIT="${LIMIT:-100}"

mkdir -p results data/logs/completion
echo "=== $(hostname) $(date)"
echo "=== model=$MODEL limit=$LIMIT"

python -u src/proof_completion.py \
  --model "$MODEL" \
  --afp "$PROJECT/src/afp-2026-06-29/thys" \
  --entries "$PROJECT/split_hol/test_entries.txt" \
  --isabelle "$HOME/isabelle-emacs/bin/isabelle" \
  --out "$OUTFILE" \
  --limit "$LIMIT" \
  --max-steps 12 \
  --states "$PROJECT/data/test_merged/states.jsonl" \
  --warmup 180 \
  --settle 20

echo "=== finished $(date)"
