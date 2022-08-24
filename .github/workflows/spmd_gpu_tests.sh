#!/bin/bash

set -x

# Print test options
echo "VERBOSE: ${VERBOSE}"
echo "SHARD: ${SHARD}"

nvidia-smi
nvcc --version
cat /etc/os-release
which python3
python3 --version
which pip3
pip3 --version

# Install git
apt-get update
apt-get install git -y

# Install dependencies
# Turn off progress bar to save logs
pip3 install --upgrade pip
if [ -f requirements.txt ]; then pip3 install -r requirements.txt --find-links https://download.pytorch.org/whl/nightly/cu113/torch_nightly.html; fi

# Install pippy
python3 spmd/setup.py install

set -ex

# Run all integration tests
# pytest --shard-id=${SHARD} --num-shards=4 --cov=spmd test/spmd/ 
pytest --shard-id=${SHARD} --num-shards=4 --cov=spmd -s test/spmd/tensor/test_tp_sharding_ops.py -k test_view_with_sharding_dim_change