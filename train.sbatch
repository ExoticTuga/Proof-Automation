#!/bin/bash
#SBATCH --job-name=qwen-sft
#SBATCH --partition=gpu.stu
#SBATCH --gres=gpu:ampere:3
#SBATCH --cpus-per-task=12
#SBATCH --mem=150G
#SBATCH --time=4-12:00:00
#SBATCH --output=slurm_logs/sft_%j.out
#SBATCH --error=slurm_logs/sft_%j.err

# ---------------------------------------------------------------------------
# Full fine-tune of Qwen2.5-Coder-7B on the harvested Isabelle proof steps.
#
#   sbatch train.sbatch
#
# This is exactly the configuration that ran successfully for 24 h (job
# 513038: 2000 steps, loss 1.3 -> 0.53, logs written throughout), with ONE
# change: --time raised from 2-00:00:00 to 4-12:00:00. That run was killed by
# the wall limit, not by any error, and gpu.stu permits 5 days.
#
# It trains from scratch. Resuming was tried and failed twice: restoring a
# checkpoint means reading ~79 GB (fp32 weights + FSDP shards + Adam state)
# over networked storage, and the job died during that read both times. A
# fresh 43 h run is more reliable than a 23 h resume that does not start.
#
# WHY THREE GPUs AND NOT FOUR
#   Cluster occupancy: no single node had four free cards. GRAD_ACCUM is set
#   so the EFFECTIVE batch (gpus x per_device x grad_accum) stays near 128
#   regardless of device count -- it is the effective batch, not the number of
#   GPUs, that determines training dynamics, so the learning rate carries over.
#
# WALL TIME
#   Measured ~36 s/step, an order of magnitude slower than a naive estimate.
#   These A100s are PCIe rather than SXM, so there is no NVLink; FSDP must
#   all-gather sharded parameters over PCIe on every forward and backward
#   pass, and that communication dominates. 4260 steps therefore need ~43 h.
# ---------------------------------------------------------------------------

set -euo pipefail

PROJECT="$HOME/Proof-Automation"
VENV="$PROJECT/.venv"
DATA="$PROJECT/data/sft"
OUT="$PROJECT/runs/qwen7b"
MODEL="Qwen/Qwen2.5-Coder-7B"

mkdir -p slurm_logs "$OUT"
source "$VENV/bin/activate"
cd "$PROJECT"

# Compute nodes are often offline; the model must already be in the cache.
# Pre-fetch on the login node with:
#   hf download Qwen/Qwen2.5-Coder-7B
export HF_HOME="${HF_HOME:-$PROJECT/.hf}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=4

echo "=== $(hostname)  $(date)"
nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader

NGPU=3
PER_DEVICE=4
# effective batch = NGPU * PER_DEVICE * GRAD_ACCUM, targeting ~128
GRAD_ACCUM=11

echo "=== $NGPU GPUs x $PER_DEVICE x $GRAD_ACCUM = $((NGPU*PER_DEVICE*GRAD_ACCUM)) effective batch"

torchrun --standalone --nproc_per_node=$NGPU src/train_sft.py \
  --model "$MODEL" \
  --data "$DATA" \
  --out "$OUT" \
  --max-len 2048 \
  --epochs 2 \
  --lr 1e-5 \
  --per-device-batch $PER_DEVICE \
  --grad-accum $GRAD_ACCUM

echo "=== finished $(date)"
