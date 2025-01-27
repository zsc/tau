#!/bin/bash
# Copyright (c) Meta Platforms, Inc. and affiliates

export MASTER_PORT=29500
export MASTER_ADDR=$(scontrol show hostname ${SLURM_NODELIST} | head -n 1)
export LOCAL_RANK=${SLURM_LOCALID}
export CUDA_VISIBLE_DEVICES=${SLURM_LOCALID}
export WORLD_SIZE=${SLURM_NTASKS}
export RANK=${SLURM_PROCID}

export USE_TQDM=0

# export NCCL_DEBUG=INFO
# export NCCL_DEBUG_SUBSYS=INIT,COLL
 export TORCH_DISTRIBUTED_DEBUG=DETAIL

python -u ddp2pipe.py --cuda=1
