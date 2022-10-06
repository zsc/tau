# Copyright (c) Meta Platforms, Inc. and affiliates
import torch
import torch.nn as nn
import functools
from spmd import (
    distribute_tensor,
    distribute_module,
    DeviceMesh,
    DTensor,
    Shard,
    Replicate,
)
from spmd.tensor.parallel import TensorParallelMultiheadAttention


def _replicate_input(inputs, device_mesh):
    DTensors = []
    for tensor in inputs:
        DTensors.append(DTensor.from_local(tensor, device_mesh, [Replicate()]))
    return tuple(DTensors)


def _aggregate_local_tensor(module: torch.nn.Module) -> torch.nn.Module:
    def hook_func(_module, _input, output):
        if isinstance(output, DTensor):
            replica_placement = [Replicate()]
            return (
                output.redistribute(output.device_mesh, replica_placement)
                .contiguous()
                .to_local()
            )

    module.register_forward_hook(hook_func)
    return module


def _gradient_hook(param, grad):
    param._local_tensor.grad = grad._local_tensor


def _shard_self_attn(name, module, device_mesh) -> None:
    col_wise_sharding = [Shard(0)]
    row_wise_sharding = [Shard(1)]
    replicate = [Replicate()]

    def _shard_self_attn_params(name, module):
        if isinstance(module, nn.Linear):
            if name == "qkv":
                sharded_weight = nn.Parameter(
                    distribute_tensor(
                        module.weight, device_mesh, col_wise_sharding
                    )
                )
                module.register_parameter("weight", sharded_weight)
                module.weight.register_hook(
                    functools.partial(_gradient_hook, module.weight)
                )
                if module.bias is not None:
                    sharded_bias = nn.Parameter(
                        distribute_tensor(
                            module.bias, device_mesh, col_wise_sharding
                        )
                    )
                    module.register_parameter("bias", sharded_bias)
                    module.bias.register_hook(
                        functools.partial(_gradient_hook, module.bias)
                    )
            elif name == "proj":
                sharded_weight = nn.Parameter(
                    distribute_tensor(
                        module.weight, device_mesh, row_wise_sharding
                    )
                )
                module.register_parameter("weight", sharded_weight)
                module.weight.register_hook(
                    functools.partial(_gradient_hook, module.weight)
                )
                _aggregate_local_tensor(module)
                if module.bias is not None:
                    replicated_bias = nn.Parameter(
                        distribute_tensor(module.bias, device_mesh, replicate)
                    )
                    module.register_parameter("bias", replicated_bias)
                    module.bias.register_hook(
                        functools.partial(_gradient_hook, module.bias)
                    )

    if isinstance(module, TensorParallelMultiheadAttention):  # shard TPMA
        for n, m in module.named_children():
            _shard_self_attn_params(n, m)
    else:
        for n, m in module.named_children():  # replace with TPMA
            if isinstance(m, nn.MultiheadAttention):
                tp_multi_head_attention = TensorParallelMultiheadAttention(
                    m.embed_dim,
                    m.num_heads,
                    device=device_mesh.device_type,
                    tp_size=device_mesh.size(0),  # group size on dim 0
                    add_bias_kv=(m.bias_k != None),
                )
                tp_multi_head_attention.copy(m)
                module.register_module(n, tp_multi_head_attention)


def shard_self_attn(device_mesh):
    return functools.partial(_shard_self_attn, device_mesh=device_mesh)


def replicate_input(device_mesh):
    return functools.partial(_replicate_input, device_mesh=device_mesh)