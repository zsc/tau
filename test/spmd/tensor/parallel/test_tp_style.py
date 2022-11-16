# Owner(s): ["oncall: distributed"]

import torch
from spmd.testing.common_dtensor import DTensorTestBase, with_comms
from spmd.tensor import distribute_tensor, DeviceMesh, Shard, Replicate
from spmd.tensor import distribute_tensor, DeviceMesh
from spmd.tensor.parallel.style import (
    make_output_shard_1d,
    make_output_replicate_1d,
    make_output_tensor,
)


class TensorParallelStyleTest(DTensorTestBase):
    # Common logic for testing prepare output funcs
    def _test_prepare_output(self, func, spec, dim=None):
        device_mesh = DeviceMesh(self.device_type, [0, 1, 2, 3])
        tensor = torch.rand(8, 16, device=self.device_type)
        dtensor = distribute_tensor(tensor, device_mesh, spec)
        if dim is not None:
            output = func(dtensor, device_mesh, dim)
        else:
            output = func(dtensor, device_mesh)
        return output, dtensor, device_mesh

    @with_comms
    def test_make_output_shard_1d(self):
        # test when output is sharded.
        output, dtensor, device_mesh = self._test_prepare_output(
            make_output_shard_1d, [Shard(0)], 1
        )
        self.assertEqual(output, dtensor.redistribute(device_mesh, [Shard(1)]))
        #  test when output is replicated.
        output, dtensor, device_mesh = self._test_prepare_output(
            make_output_shard_1d, [Replicate()], 0
        )
        self.assertEqual(output, dtensor.redistribute(device_mesh, [Shard(0)]))

    @with_comms
    def test_make_output_replicate_1d(self):
        output, dtensor, device_mesh = self._test_prepare_output(
            make_output_replicate_1d, [Shard(0)]
        )
        self.assertEqual(
            output, dtensor.redistribute(device_mesh, [Replicate()])
        )

    @with_comms
    def test_make_output_tensor(self):
        # test when output is sharded.
        output, dtensor, device_mesh = self._test_prepare_output(
            make_output_tensor, [Shard(0)]
        )
        self.assertEqual(
            output, dtensor.redistribute(device_mesh, [Replicate()]).to_local()
        )
        #  test when output is replicated.
        output, dtensor, device_mesh = self._test_prepare_output(
            make_output_tensor, [Replicate()]
        )
        self.assertEqual(
            output, dtensor.redistribute(device_mesh, [Replicate()]).to_local()
        )

    # Common logic for testing prepare output funcs errors.
    def _test_prepare_output_error(self, func):
        tensor = torch.rand(8, 16, device=self.device_type)
        device_mesh = DeviceMesh(self.device_type, [0, 1, 2, 3])
        dtensor = distribute_tensor(tensor, device_mesh, [Shard(0)])
        output = [dtensor]
        with self.assertRaisesRegex(
            AssertionError,
            f"output of Tensor Parallel is actually {type(output)} not DTensor.",
        ):
            func(output, device_mesh)
        device_mesh = DeviceMesh(self.device_type, [[0, 1], [2, 3]])
        with self.assertRaisesRegex(
            AssertionError, f"{func.__name__}: device mesh is not 1D"
        ):
            func(dtensor, device_mesh)

    @with_comms
    def test_prepare_output_error(self):
        self._test_prepare_output_error(make_output_shard_1d)
        self._test_prepare_output_error(make_output_replicate_1d)
        self._test_prepare_output_error(make_output_tensor)
