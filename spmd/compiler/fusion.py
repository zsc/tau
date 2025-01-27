import operator
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, cast

import torch
import torch.fx as fx
from torch.distributed.distributed_c10d import ReduceOp, _get_default_group
from torch.fx.experimental.proxy_tensor import make_fx
from torch.fx.passes.shape_prop import TensorMetadata

from spmd.compiler.log_utils import get_logger

from .graph_utils import (
    OP,
    CommType,
    get_comm_block_nodes,
    get_node_tensor_metadata,
    get_output_node,
    rebuild_graph,
)


@dataclass
class FusionElement:
    """
    This class tracks the nodes for a DTensor expanded communication collective
    in the graph.
    """

    # Monitor if this FusionElement is in the main graph or removed as part of
    # fusion.
    in_graph: bool = False
    # Has gone through the fusion policy process
    processed: bool = False
    size: int = 0
    shape: Optional[torch.Size] = None
    comm_type: Optional[CommType] = None
    node_list: List[fx.Node] = field(default_factory=lambda: [])  # type: ignore
    # Node that was before start of the section.
    prev_node: Optional[fx.Node] = None
    output_name: str = ""
    comm_node: Optional[fx.Node] = None
    wait_node: Optional[fx.Node] = None
    grad_tensor_node: Optional[fx.Node] = None

    def _get_next_node(self) -> fx.Node:
        """Get the next node after this FE section"""
        next_node = self.node_list[-1].next
        assert (
            next_node is not None
        ), f"failed to get valid next node after {self.node_list[-1].name}"
        return next_node


@dataclass
class GraphInfo:
    """Provides a home for global aspects of this graph.
    Currently tracks first and last node, len of the graph and
    the location and size of the global buffer node
    """

    # starting len of the graph
    len: int = 0
    # total count of initial fusion elements
    num_starting_fe: int = 0
    # list of all FusionElements in the graph
    fe_list: List[FusionElement] = field(default_factory=lambda: [])
    # max memory needed for fusion buffer
    peak_memory_required: int = 0
    # list housing global buffers for fusion comms
    _ring_buffer: Optional[List[fx.Node]] = None
    # size of the global buffer
    global_buffer_size: int = 0
    _ring_num_buffers: int = 0
    _ring_index: int = 0
    _current_ring_index: int = 0
    # real buffer (not node) used for tracing fusion subgraphs
    tracing_buffer: Optional[torch.Tensor] = None
    # first node in graph (head)
    first: Optional[fx.Node] = None
    # last node in graph (tail / output)
    output: Optional[fx.Node] = None
    # offset to comm node within a FusionElement sequence
    fe_offset_to_comm_node: Optional[int] = None
    # Map from the wait_node to
    wait_node_idx: Dict[fx.Node, int] = field(default_factory=lambda: {})
    # The gradient to index in the graph.nodes(). This index will change after
    # any transformation but we need this to get the order of the gradient.
    actual_grad_index_mapping: Dict[fx.Node, int] = field(
        default_factory=lambda: {}
    )
    global logger
    logger = get_logger("graph_opt")  # type: ignore

    def setup_ring_buffer(
        self, buffer_node_list: List[fx.Node], buffer_size: int
    ) -> None:
        """init ring buffer for sequential allocation"""
        self._ring_buffer = buffer_node_list
        self._ring_num_buffers = len(self._ring_buffer)
        self._ring_index = 0
        self.global_buffer_size = buffer_size

    def get_next_ring_buffer(
        self,
    ) -> fx.Node:
        """get the next buffer node in the ring"""
        buffer_node = self._ring_buffer[self._ring_index]  # type: ignore
        self._current_ring_index = self._ring_index
        assert (
            buffer_node is not None
        ), f"failed to get ring buffer for index {self._ring_index}\n"
        self._ring_index += 1
        if self._ring_index >= self._ring_num_buffers:
            self._ring_index = 0
        return buffer_node

    def get_current_ring_buffer(
        self,
    ) -> fx.Node:
        """used to retrieve the current one (for remapping)"""
        return self._ring_buffer[self._current_ring_index]  # type: ignore

    def update_info(self, gm: fx.GraphModule) -> "GraphInfo":
        """Get the len, input and output nodes"""
        graph_len = gm.graph._len
        if not graph_len:
            raise ValueError("Empty graph passed in....")
        self.len = graph_len

        nodelist = gm.graph.nodes

        for i, node in enumerate(nodelist):
            if node.op == OP.PLACEHOLDER and self.first is not None:
                self.first = node

            if node.op == OP.OUTPUT:
                for i, arg in enumerate(node.args[0]):
                    if isinstance(arg, fx.Node) and arg.name.startswith(
                        "wait_comm"
                    ):
                        self.wait_node_idx[arg] = i

        self.output = get_output_node(gm)
        assert (
            self.output is not None
        ), f"Unable to locate output node in gm {gm.graph}"

        logger.debug(  # type: ignore
            f"Updated graph_info - len = {self.len} input = {self.first}, output = {self.output}",
        )
        return self


def _create_fusion_buffers(
    gm: fx.GraphModule,
    buffer_size: int,
    gi: Optional[GraphInfo],
    ring_size: int,
) -> List[fx.Node]:
    """Insert torch.empty node(s) for the global buffer.
    defaults to first node after placeholder nodes.
    appends to GlobalInfo if passed in"""

    # default to inserting just after last placeholder node
    # TODO - more efficient if we drop the buffer right before first use
    # to reduce memory pressure.
    for node in gm.graph.nodes:
        if node.op == OP.PLACEHOLDER:
            continue
        insert_before_node = node
        break

    ring_buffer = []
    new_buffer_node = None
    # there is an assumption below that torch.set_device has been setup by
    # DTensor.  We thus ride on that by passing "cuda" for device, which
    # should expand internally to "cuda:index".
    with gm.graph.inserting_before(insert_before_node):
        for i in range(ring_size):
            new_buffer_node = gm.graph.create_node(
                OP.CALL_FUNCTION,
                target=torch.empty,
                args=(buffer_size,),
                kwargs={"device": "cuda"},
            )
            ring_buffer.append(new_buffer_node)

    assert (
        new_buffer_node is not None
    ), f"failed to create buffer node, size={buffer_size}"

    # init ring buffer
    gi.setup_ring_buffer(ring_buffer, buffer_size)  # type: ignore

    return ring_buffer


def _scan_graph_for_fusion_elements(
    gi: GraphInfo,
    gm: fx.GraphModule,
    comm_type: CommType = CommType.ALLREDUCE,
) -> List[FusionElement]:
    """Scan entire graph for matching sections of CommTensor style expansions
    returns list of FusionElements that match CommType"""
    logger = get_logger("graph_opt")

    element_list = []
    for node in gm.graph.nodes:
        if node.name.startswith("wait_comm"):
            comm_idx, comm_block_nodes = get_comm_block_nodes(node, comm_type)
            comm_node = comm_block_nodes[comm_idx]
            grad_node = cast(Tuple[fx.Node, ...], comm_node.args[0])[0]
            tmeta = get_node_tensor_metadata(grad_node)
            fe = FusionElement(
                comm_type=comm_type,
                node_list=comm_block_nodes[:],
                # Need to fully populate this fe. We will be
                # revoing/rewriting the node list so we save prev and next.
                prev_node=comm_block_nodes[0].prev,
                output_name=node.name,
                wait_node=node,
                comm_node=comm_node,
                grad_tensor_node=grad_node,
                size=tmeta.shape.numel(),
                shape=tmeta.shape,
            )
            element_list.append(fe)
            # ensure we have global index to comm_node
            if not gi.fe_offset_to_comm_node:
                len_comm_section = len(fe.node_list)
                gi.fe_offset_to_comm_node = len_comm_section - comm_idx - 1
                logger.debug(  # type: ignore
                    f"global comm index set {gi.fe_offset_to_comm_node}\n"
                )
    return element_list


def _copy_fe_to_buffer(
    gi: GraphInfo, gm: fx.GraphModule, copy_list: List[FusionElement]
) -> None:
    """First half of fusion - move desired items to buffer and create graph"""
    logger = get_logger("graph_opt")

    buffer_node = gi.get_next_ring_buffer()
    buffer_size = gi.global_buffer_size

    num_fusion_elements = len(copy_list)

    logger.info(f"_copy_fe_to_buffer {num_fusion_elements=}")  # type: ignore

    def copy_to_buffer(
        concat_buffer: torch.Tensor, tensor_list: List[torch.Tensor]
    ) -> torch.Tensor:
        offset = 0
        for t in tensor_list:
            size = t.numel()
            concat_buffer[offset : offset + size] = t.view(-1)
            offset += size
        return concat_buffer

    # setup dummy vars
    buffer = None
    if gi.tracing_buffer is None:
        buffer = torch.empty(buffer_size)
        gi.tracing_buffer = buffer
    else:
        buffer = gi.tracing_buffer

    tlist = []
    for item in copy_list:
        a = torch.zeros(item.shape)  # type: ignore
        tlist.append(a)

    load_gm = make_fx(copy_to_buffer)(buffer, tlist)
    # update load loop to use main graph items
    fn_list = []
    pl_list = []
    for node in load_gm.graph.nodes:
        if node.op == OP.PLACEHOLDER:
            pl_list.append(node)
        elif node.op == OP.CALL_FUNCTION:
            fn_list.append(node)

    # create placeholder remapping
    pl_map: Dict[fx.Node, fx.Node] = {}
    pl_map[pl_list[0]] = buffer_node  # type: ignore

    for i in range(num_fusion_elements):
        # pl map remaps traced placeholders used in copy graph to main graph grad tensors
        pl_map[pl_list[i + 1]] = copy_list[i].grad_tensor_node  # type: ignore

    insert_node = copy_list[-1].comm_node
    value_remap: Dict[fx.Node, fx.Node] = {}

    def remap_copy_args(in_node: fx.Node) -> fx.Node:
        out_node = in_node
        if in_node in pl_map:
            out_node = pl_map[in_node]  # type: ignore
        elif in_node in value_remap:
            out_node = value_remap[in_node]
        return out_node

    # overlap - move the new gather section to the source node
    all_grad_nodes = []
    for fe in copy_list:
        assert fe.grad_tensor_node is not None
        assert fe.grad_tensor_node.name.startswith("clone")
        all_grad_nodes.append(fe.grad_tensor_node)

    grad_indices_mapping = [
        gi.actual_grad_index_mapping[
            cast(Tuple[fx.Node], grad_tensor_node.args)[0]
        ]
        for grad_tensor_node in all_grad_nodes
    ]

    last_grad_fe_index = grad_indices_mapping.index(max(grad_indices_mapping))
    assert copy_list[last_grad_fe_index].grad_tensor_node is not None
    last_grad_tensor_node = cast(
        fx.Node,
        cast(fx.Node, copy_list[last_grad_fe_index].grad_tensor_node).args[0],
    )
    source_node = last_grad_tensor_node  # get_source_node_next(insert_node)

    logger.info(
        f"copy buffer to start =  {source_node.name}\n {all_grad_nodes=}"
    )

    # move clone nodes
    curr_node = source_node
    for item in all_grad_nodes:  # type: ignore
        if curr_node.next is not item:
            curr_node.append(item)
        curr_node = curr_node.next

    logger.info(f"After clone node {gm.graph.print_tabular()}")

    # move final tensor_constants
    constant_list = [copy_list[-1].node_list[1], copy_list[-1].node_list[2]]

    assert constant_list[0].name.startswith(
        "_tensor_constant"
    ), f"failed to locate tensor constant node {constant_list[0]}"
    assert constant_list[1].name.startswith(
        "_tensor_constant"
    ), f"failed to locate tensor constant node {constant_list[1]}"

    for item in constant_list:  # type: ignore
        curr_node.append(item)
        curr_node = curr_node.next

    # move all_reduce final node
    buffer_comm_node = copy_list[-1].comm_node
    buffer_comm_node.update_arg(0, [buffer_node])  # type: ignore
    curr_node.append(buffer_comm_node)
    curr_node = curr_node.next

    nodes_inserted_count = 0
    with gm.graph.inserting_before(curr_node):
        for innernode in load_gm.graph.nodes:
            nodes_inserted_count += 1
            if innernode.op in [OP.PLACEHOLDER, OP.OUTPUT]:
                continue
            value_remap[innernode] = gm.graph.node_copy(
                innernode, remap_copy_args
            )

    _update_new_copy_nodes_users(value_remap)

    gm.recompile()
    logger.info(
        f"After clone, tensor_constant and allreduce insert {gm.graph.print_tabular()}"
    )


def _build_buffer_comm_graph(
    gi: GraphInfo, gm: fx.GraphModule
) -> fx.GraphModule:
    """This function is only a stub atm, for cases where we have
    to make our own all_reduce and wait subgraph for buffer. Wrapping with
    CommTensor is required to complete.
    """
    buffer_size = gi.global_buffer_size

    def dummy_add(
        grad_buffer: torch.Tensor, zero: torch.Tensor
    ) -> torch.Tensor:
        return grad_buffer + zero

    grad_buffer: torch.Tensor = torch.empty(buffer_size)
    zero: torch.Tensor = torch.zeros_like(grad_buffer)

    traced_add = make_fx(dummy_add)(grad_buffer, zero)

    # TODO - needs to match to DTensor PG
    pg = _get_default_group()
    tensor: torch.Tensor
    op: ReduceOp = ReduceOp.SUM  # type: ignore[assignment]
    async_op: bool = False

    return traced_add


def _scatter_results_from_buffer(
    gi: GraphInfo, gm: fx.GraphModule, fe_list: List[FusionElement]
) -> None:
    """After comm event with buffer, scatter results back to original fe grad tensors"""

    buffer_node = gi.get_current_ring_buffer()
    buffer_size = gi.global_buffer_size

    scatter_list = fe_list
    num_fe_items = len(scatter_list)

    def scatter_from_buffer(
        buffer: torch.Tensor, scatter_list: List[torch.Tensor]
    ) -> torch.Tensor:
        offset = 0
        for t in scatter_list:
            numel = t.numel()
            shaper = buffer[offset : offset + numel].view(t.shape)
            t.copy_(shaper)
            offset += numel
        return buffer

    buffer = gi.tracing_buffer
    assert buffer is not None, f" missing global tracing buffer in {gi}"
    buffer_shape = buffer.shape

    tlist = []
    for item in scatter_list:
        a = torch.zeros(item.shape)  # type: ignore
        tlist.append(a)

    scatter_sg = make_fx(scatter_from_buffer)(buffer, tlist)
    pl_list = []

    for node in scatter_sg.graph.nodes:
        if node.op == OP.PLACEHOLDER:
            pl_list.append(node)

    insert_node = fe_list[-1]._get_next_node()  # before last node of FE section

    # create placeholder remapping
    pl_map: Dict[fx.Node, fx.Node] = {}
    pl_map[pl_list[0]] = buffer_node  # type: ignore
    for i in range(num_fe_items):
        pl_map[pl_list[i + 1]] = fe_list[i].grad_tensor_node  # type: ignore

    update_node_user_count: Dict[fx.Node, str] = {}
    value_remap: Dict[fx.Node, fx.Node] = {}

    def remap_scatter_args(in_node: fx.Node) -> fx.Node:
        out_node = in_node
        if in_node in pl_map:
            out_node = pl_map[in_node]  # type: ignore
        elif in_node in value_remap:
            out_node = value_remap[in_node]

        update_node_user_count[out_node] = ""
        return out_node

    with gm.graph.inserting_before(insert_node):
        for innernode in scatter_sg.graph.nodes:
            if innernode.op in [OP.PLACEHOLDER, OP.OUTPUT]:
                continue
            value_remap[innernode] = gm.graph.node_copy(
                innernode, remap_scatter_args
            )

    # insert into main graph, just above last fe

    # force copies and waits to have a user
    # copies and waits do not have users by default, and will be
    # removed at recompile (can lead to lots of surprise/frustration)
    # TODO this does not account for nodes beyond our own...remove/fix this

    _update_new_copy_nodes_users(value_remap)

    # also must update wait for the scatter section
    section_wait_node = scatter_list[-1].wait_node
    user = section_wait_node.args[0]  # type: ignore
    section_wait_node.users[user] = ""  # type: ignore
    wait_node_user_count = len(section_wait_node.users)  # type: ignore

    assert (
        wait_node_user_count > 0
    ), f"failed to update users for node {node.name}"

    # finally, need to update the graph TensorMetadata info (not a must, but ensures well formed graph)

    last_get_item_node = scatter_list[-1].wait_node.args[0]  # type: ignore
    tensor_meta = last_get_item_node.meta.get("tensor_meta", None)  # type: ignore
    assert (
        tensor_meta is not None
    ), f"failed to get tensor metadata for last getitem node {last_get_item_node=}"

    # replace with buffer metadata
    buffer_meta = buffer_node.meta.get("tensor_meta", None)  # type: ignore

    new_tensor_meta = _update_node_tensor_metadata(
        last_get_item_node, new_shape=buffer_shape  # type: ignore
    )

    gm.recompile()


def _update_new_copy_nodes_users(value_remap: Dict[fx.Node, fx.Node]) -> None:
    """
    We have to manually update users for new copy nodes to ensure count > 0.
    This seems to be an fx bug, but for now we update or else fusion will get removed during graph linting
    """
    for subnode, node in value_remap.items():
        if node.name.startswith("copy"):
            user = node.args[0]
            node.users[user] = ""  # type: ignore
            node_user_len = len(node.users)
            assert node_user_len, f"failed to update users for node {node.name}"


def _update_node_tensor_metadata(
    node: fx.Node,
    new_shape: torch.Size,
    in_dtype: Optional[torch.dtype] = None,
    in_memory_format: Optional[torch.memory_format] = None,
) -> TensorMetadata:
    """Update a node's metadata to the the new shape, dtype and/or memory format"""
    curr = node.meta.get("tensor_meta")
    assert (
        curr is not None
    ), f"failed to obtain tensor meta data on node {node.name}"

    shape = curr.shape
    curr_dtype = curr.dtype
    requires_grad = curr.requires_grad
    stride = curr.stride

    curr_memory_format = curr.memory_format
    is_quantized = curr.is_quantized
    qparams = curr.qparams

    updated_dtype = in_dtype if in_dtype is not None else curr_dtype
    updated_memory_format = (
        in_memory_format if in_memory_format is not None else curr_memory_format
    )

    new_metadata = TensorMetadata(
        new_shape,
        updated_dtype,
        requires_grad,
        stride,
        updated_memory_format,
        is_quantized,
        qparams,
    )

    # update meta with new TensorMetadata
    saved_meta = node.meta.get("tensor_meta")
    node.meta["tensor_meta"] = new_metadata

    return new_metadata


def _finalize_output_node(
    gi: GraphInfo,
    gm: fx.GraphModule,
    fe_list: List[FusionElement],
    start: int,
    stop: int,
    new_output_args: List[fx.Node],
) -> None:
    """Reworks output node args to original grad tensors, replacing the wait_comms
    we update a copy of the output args, then finalized after all fusion is done."""
    replacement_mapping: Dict[fx.Node, fx.Node] = {}

    # map out all updated nodes in our list
    # working in reverse for fusion, so undo for simple replacement
    # fe_list = fe_list[::-1]
    for item in fe_list:
        grad_node = item.grad_tensor_node
        wait_node = item.wait_node
        replacement_mapping[wait_node] = grad_node  # type: ignore

    # we have fused a subset, only update that subset within the larger output node args
    # TODO - this assumes that all gradient tensors are comm handled.
    for i in range(len(fe_list)):
        index = start + i
        curr_node = new_output_args[index]

        if curr_node is not None:
            assert curr_node.name.startswith(
                "wait"
            ), f"Non comm gradient output tensor incorrectly handled...needs fix. {new_output_args[start+i]}"
            new_output_args[start + i] = replacement_mapping[curr_node]

    logger.info(f"Updated output args = {new_output_args}")  # type: ignore


def _determine_peak_memory(gi: GraphInfo, fusion_length: int) -> int:
    """
    Scans fe list to determine max memory required across all fusion instances.
    this result is used to allocate the global buffer for fusion, where we
    re-use a global buffer to avoid repeated allocations per fusion.
    """
    peak_memory = 0  # currently measured in numel
    curr_memory = 0
    curr_fe_index = 0

    for item in gi.fe_list:  # type: ignore
        curr_fe_index += 1
        curr_memory += item.size  # type: ignore

        if curr_fe_index == fusion_length:
            peak_memory = max(peak_memory, curr_memory)
            curr_fe_index = 0
            curr_memory = 0

    logger.info(f"peak memory determined to be {peak_memory}")  # type: ignore
    gi.peak_memory_required = peak_memory

    return peak_memory


def _setup(gm: fx.GraphModule) -> GraphInfo:
    """shared setup for optimizations"""

    # first recompile to make sure we have coherent graph
    gm.recompile()

    # get our main graph info
    graph_info = GraphInfo()
    graph_info.update_info(gm)

    return graph_info


def _teardown(gm: fx.GraphModule) -> None:
    """final steps before exiting optimization phase"""
    rebuild_graph(gm)
    logger.info(f"Final Graph {gm.graph.print_tabular()}")  # type: ignore


def run_fuse_communication_ring(
    gm: fx.GraphModule,
    fusion_length: int,
    ring_num_buffers: int,
) -> None:
    """fusion using a ring buffer in order to avoid buffer overwriting"""
    logger = get_logger("graph_opt")

    assert (
        fusion_length > 1
    ), f"fusion policy is {fusion_length}, but requires > 1 for actual fusion. "

    logger.info(  # type: ignore
        f"Start of fusion_ring pass, fusion_length = {fusion_length}, buffers = {ring_num_buffers}"
    )

    graph_info = _setup(gm)

    # scan graph for all comm sections (fusion elements)
    fe_list = _scan_graph_for_fusion_elements(
        graph_info, gm, comm_type=CommType.ALLREDUCE
    )

    graph_info.num_starting_fe = len(fe_list)  # type: ignore
    logger.info(f"len of fe_list = {len(fe_list)}")

    graph_info.fe_list = fe_list

    # determine peak memory using fusion policy
    peak_memory_required = _determine_peak_memory(graph_info, fusion_length)

    assert (
        peak_memory_required > 0
    ), f"failed to compute effective peak memory - determined {peak_memory_required} as buffer size\n"

    ring_buffer = _create_fusion_buffers(
        gm, peak_memory_required, graph_info, ring_num_buffers
    )
    assert len(graph_info.wait_node_idx) == len(fe_list), (
        "The expected wait_nodes in graph_info are different from fe_list "
        f"{len(graph_info.wait_node_idx)} {len(fe_list)}."
    )
    assert graph_info.output is not None
    new_output_args = list(cast(Tuple[fx.Node], graph_info.output.args[0]))

    # track the index of the grad nodes in the graph so we can pull the
    # correct "last" gradient node from any given fusion set.
    # TODO - shared function here
    actual_gradients = set(
        cast(Tuple[fx.Node], cast(fx.Node, fe.grad_tensor_node).args)[0]
        for fe in fe_list
    )
    for idx, node in enumerate(gm.graph.nodes):
        if node in actual_gradients:
            graph_info.actual_grad_index_mapping[node] = idx

    # Main processing loop
    for start in range(0, len(graph_info.fe_list), fusion_length):
        stop = start + fusion_length
        to_fuse_fe_list = graph_info.fe_list[start:stop]

        _copy_fe_to_buffer(graph_info, gm, to_fuse_fe_list)

        _scatter_results_from_buffer(graph_info, gm, to_fuse_fe_list)

        _finalize_output_node(
            graph_info,
            gm,
            to_fuse_fe_list,
            start,
            stop,
            new_output_args,
        )

    # update output with the updated args
    gm.graph.erase_node(graph_info.output)
    gm.graph.output(new_output_args)

    logger.info(f"Ring Comm Fusion processed {len(fe_list)} fe items")

    rebuild_graph(gm)


def _get_source_node_next(comm_node: fx.Node) -> fx.Node:
    """determine source gradient node from a given comm node.
    Returns the next (prepend) node in the graph to prepare for insert.
    """

    curr_source = comm_node.args[0][0]  # type: ignore

    # if clone, find clone source
    if curr_source.name.startswith("clone"):  # type: ignore
        clone_source = curr_source.args[0]  # type: ignore
        curr_source = clone_source  # type: ignore

    prepend_node = curr_source.next  # type: ignore

    assert (
        prepend_node is not None
    ), f"failed to get next from {curr_source.name}"  # type: ignore

    return prepend_node


def _move_comm_section(
    gi: GraphInfo, gm: fx.GraphModule, fe: FusionElement
) -> Optional[List[fx.Node]]:
    """find source node for comm node"""

    prepend_node = _get_source_node_next(fe.comm_node)  # type: ignore
    # we are moving the uppper section (comm node and support nodes) only
    nodes_to_move = fe.node_list[0 : gi.fe_offset_to_comm_node]  # type: ignore

    for item in nodes_to_move:
        prepend_node.prepend(item)

    return nodes_to_move


def _fuse_with_cat(
    gi: GraphInfo, gm: fx.GraphModule, copy_list: List[FusionElement]
) -> fx.Node:
    # Find the actual last gradient.
    all_grad_tensor_nodes = []
    for fe in copy_list:
        assert fe.grad_tensor_node is not None
        assert fe.grad_tensor_node.name.startswith("clone")
        all_grad_tensor_nodes.append(fe.grad_tensor_node)
    grad_indices_mapping = [
        gi.actual_grad_index_mapping[
            cast(Tuple[fx.Node], grad_tensor_node.args)[0]
        ]
        for grad_tensor_node in all_grad_tensor_nodes
    ]
    last_grad_fe_index = grad_indices_mapping.index(max(grad_indices_mapping))
    assert copy_list[last_grad_fe_index].grad_tensor_node is not None
    last_grad_tensor_node = cast(
        fx.Node,
        cast(fx.Node, copy_list[last_grad_fe_index].grad_tensor_node).args[0],
    )

    with gm.graph.inserting_after(last_grad_tensor_node):
        cat_inputs = [
            gm.graph.call_function(
                torch.flatten,
                (cast(fx.Node, cast(fx.Node, fe.grad_tensor_node).args[0]),),
            )
            for fe in copy_list
        ]

    with gm.graph.inserting_after(cat_inputs[0]):
        cat_node = gm.graph.call_function(torch.cat, (cat_inputs,))

    assert copy_list[-1].comm_node is not None
    fused_comm_node = copy_list[-1].comm_node
    assert fused_comm_node is not None, "Pyre is not as smart as Mypy."
    fused_comm_node.update_arg(0, [cat_node])

    # Move the fused_comm_node and its args to right after the source node
    nodes_to_move = [
        fused_comm_node,
        fused_comm_node.args[1],
        fused_comm_node.args[2],
        cat_node,
    ] + cat_inputs
    for node in nodes_to_move:
        last_grad_tensor_node.append(node)

    return fused_comm_node


def _scatter_results(
    gi: GraphInfo, gm: fx.GraphModule, scatter_list: List[FusionElement]
) -> List[fx.Node]:
    scatter_sizes = [fe.size for fe in scatter_list]
    assert scatter_list[-1].wait_node is not None
    wait_node = scatter_list[-1].wait_node
    with gm.graph.inserting_after(wait_node):
        scatter_node = gm.graph.call_function(
            torch.split,
            (wait_node, scatter_sizes),
        )

    grad_nodes = []
    with gm.graph.inserting_after(scatter_node):
        for idx, fe in enumerate(scatter_list):
            grad_node = gm.graph.call_function(
                operator.getitem, (scatter_node, idx)
            )
            with gm.graph.inserting_after(grad_node):
                grad_nodes.append(
                    gm.graph.call_function(torch.reshape, (grad_node, fe.shape))
                )

    return grad_nodes


def _update_output_args(
    gi: GraphInfo,
    gm: fx.GraphModule,
    fe_list: List[FusionElement],
    output_args: List[fx.Node],
    grad_nodes: List[fx.Node],
) -> None:
    for fe, grad_node in zip(fe_list, grad_nodes):
        assert fe.wait_node is not None
        output_args[gi.wait_node_idx[fe.wait_node]] = grad_node


def run_fuse_communication_cat(gm: fx.GraphModule, fusion_length: int) -> None:
    """
    Run fuse communication with concat.
    This implementation use concat to concat the bucketed gradients.
    """
    # First recompile to make sure we have coherent graph
    gm.recompile()

    graph_info = GraphInfo().update_info(gm)

    fe_list = _scan_graph_for_fusion_elements(
        graph_info, gm, comm_type=CommType.ALLREDUCE
    )
    graph_info.fe_list = fe_list
    assert len(graph_info.wait_node_idx) == len(fe_list), (
        "The expected wait_nodes in graph_info is different from fe_list "
        f"{len(graph_info.wait_node_idx)} {len(fe_list)}."
    )
    assert graph_info.output is not None
    new_output_args = list(cast(Tuple[fx.Node], graph_info.output.args[0]))

    # Need this mapping because the gradient may not have the same order
    # as clone.
    actual_gradients = set(
        cast(Tuple[fx.Node], cast(fx.Node, fe.grad_tensor_node).args)[0]
        for fe in fe_list
    )
    for idx, node in enumerate(gm.graph.nodes):
        if node in actual_gradients:
            graph_info.actual_grad_index_mapping[node] = idx

    # Fuse every ``fusion_length`` FusionElement.
    for start in range(0, len(graph_info.fe_list), fusion_length):
        fe_list = graph_info.fe_list[start : (start + fusion_length)]
        fused_comm_node = _fuse_with_cat(graph_info, gm, fe_list)
        grad_nodes = _scatter_results(graph_info, gm, fe_list)
        _update_output_args(
            graph_info,
            gm,
            fe_list,
            new_output_args,
            grad_nodes,
        )

    # update output with the updated args
    gm.graph.erase_node(graph_info.output)
    gm.graph.output(new_output_args)
    rebuild_graph(gm)


def _map_local_gradients(
    gm: fx.GraphModule, graph_info: GraphInfo, fe_list: List[FusionElement]
) -> None:
    """map gradient tensors to index within the graph.  This is needed b/c
    sometimes clones are out of order and we want to use the actual 'last'
    gradient tensor within a set for fusion"""
    actual_gradients = set(
        cast(Tuple[fx.Node], cast(fx.Node, fe.grad_tensor_node).args)[0]
        for fe in fe_list
    )
    for idx, node in enumerate(gm.graph.nodes):
        if node in actual_gradients:
            graph_info.actual_grad_index_mapping[node] = idx


def _get_last_grad_node_from_fe_group(
    gi: GraphInfo, copy_list: List[FusionElement]
) -> int:
    """given a subset of FusionElements, find the index of the last
    gradient node, where last = actual graph order"""

    all_grad_tensor_nodes = []
    for fe in copy_list:
        assert fe.grad_tensor_node is not None
        assert fe.grad_tensor_node.name.startswith("clone")
        all_grad_tensor_nodes.append(fe.grad_tensor_node)

    grad_index_mapping = [
        gi.actual_grad_index_mapping[
            cast(Tuple[fx.Node], grad_tensor_node.args)[0]
        ]
        for grad_tensor_node in all_grad_tensor_nodes
    ]

    last_grad_fe_index = grad_index_mapping.index(max(grad_index_mapping))
    assert copy_list[last_grad_fe_index].grad_tensor_node is not None
    return last_grad_fe_index


def _fuse_with_jit(
    gi: GraphInfo, gm: fx.GraphModule, copy_list: List[FusionElement]
) -> fx.Node:

    # Find the actual last gradient node.
    last_grad_fe_index = _get_last_grad_node_from_fe_group(gi, copy_list)

    last_grad_tensor_node = cast(
        fx.Node,
        cast(fx.Node, copy_list[last_grad_fe_index].grad_tensor_node).args[0],
    )

    jit_inputs = []
    offset = 0
    size = 0
    copy_nodes = []

    buffer_size_needed = sum([item.size for item in copy_list])  # type: ignore

    device = torch.cuda.current_device()
    gpu = "cuda:" + str(device)

    jit_buffer_node = gm.graph.create_node(
        OP.CALL_FUNCTION,
        target=torch.empty,
        args=(buffer_size_needed,),
        kwargs={"device": gpu},
    )
    jit_inputs.append(jit_buffer_node)

    for fe in copy_list:
        start = offset
        stop = offset + fe.size
        # jump from clone to actual tensor node
        grad_tensor_clone_node = cast(fx.Node, fe.grad_tensor_node)
        grad_node = grad_tensor_clone_node.args[0]

        view_node = gm.graph.call_function(
            torch.ops.aten.view.default,
            (
                grad_node,
                [-1],
            ),
        )

        slice_node = gm.graph.call_function(
            torch.ops.aten.slice.Tensor,
            (jit_buffer_node, 0, start, stop),
        )
        copy_node = gm.graph.call_function(
            # torch.Tensor.copy_,
            torch.ops.aten.copy_.default,
            (slice_node, view_node),
        )
        offset += fe.size
        jit_inputs.extend([view_node, slice_node, copy_node])
        copy_nodes.append(copy_node)

    assert copy_list[-1].comm_node is not None

    fused_comm_node = copy_list[-1].comm_node  # type: ignore

    fused_comm_node.update_arg(0, [jit_buffer_node])  # type: ignore

    fused_comm_node.users[jit_buffer_node] = ""  # type: ignore

    # Move the fused_comm_node and its args to right after the source node
    nodes_to_move = jit_inputs + [
        fused_comm_node.args[1],  # type: ignore
        fused_comm_node.args[2],  # type: ignore
        fused_comm_node,
    ]  # type: ignore

    insert_node = last_grad_tensor_node.next
    for node in nodes_to_move:
        insert_node.prepend(node)

    # enforce users count
    for node in copy_nodes:
        user = node.args[0]
        node.users[user] = ""  # type: ignore
        node_user_len = len(node.users)
        assert node_user_len, f"failed to update users for node {node.name}"

    return jit_buffer_node


def _scatter_results_jit(
    gi: GraphInfo,
    gm: fx.GraphModule,
    scatter_list: List[FusionElement],
    jit_buffer_node: fx.Node,
) -> List[fx.Node]:
    """prepare views against completed buffer, these will replace gradient nodes for
    graph output to avoid copy back overhead."""

    assert scatter_list[-1].wait_node is not None
    wait_node = scatter_list[-1].wait_node

    # ensure user

    wait_user = wait_node.args[0]  # type: ignore
    wait_node.users[wait_user] = ""  # type: ignore

    scatter_nodes = []

    grad_nodes = []

    offset = 0
    start, stop = 0, 0
    shape: torch.Size = None  # type: ignore

    for fe in scatter_list:
        start = offset
        stop = start + fe.size
        shape = cast(torch.Size, fe.shape)

        slice_node = gm.graph.call_function(
            torch.ops.aten.slice.Tensor,
            (jit_buffer_node, 0, start, stop),
        )

        view_node = gm.graph.call_function(
            torch.ops.aten.view.default,
            (slice_node, shape),
        )

        offset += fe.size

        # ensure user for view node
        user = slice_node
        view_node.users[user] = ""  # type: ignore
        # ensure for copy_node
        # user = copy_out_node.args[0]
        # copy_out_node.users[user] = ""

        scatter_nodes.extend([slice_node, view_node])

        grad_nodes.append(view_node)

    # move nodes
    insert_node = wait_node.next  # type: ignore
    for node in scatter_nodes:
        insert_node.prepend(node)

    return grad_nodes


def run_fuse_communication_jit(gm: fx.GraphModule, fusion_length: int) -> None:

    """runs fusion by creating a Just in Time buffer to use for each fusion.
    It then returns views to the buffer for the gradient outputs, avoiding the
    need to copy back to the original tensor.
    We can thus del the gradient tensors as fused, and by only creating buffers as
    needed, minimize overhead memory pressure."""
    FP32_BYTES = 4

    gm.recompile()
    graph_info = GraphInfo().update_info(gm)

    fe_list = _scan_graph_for_fusion_elements(
        graph_info, gm, comm_type=CommType.ALLREDUCE
    )

    graph_info.fe_list = fe_list

    assert len(graph_info.wait_node_idx) == len(fe_list), (
        "The expected wait_nodes in graph_info are different from the fe_list "
        f"{len(graph_info.wait_node_idx)} {len(fe_list)}."
    )

    assert graph_info.output is not None
    new_output_args = list(cast(Tuple[fx.Node], graph_info.output.args[0]))

    _map_local_gradients(gm, graph_info, fe_list)

    # use fusion length as mb instead of count
    start = 0
    stop = 0

    bucket_size = fusion_length * 1024 * 1024
    curr_size = 0

    for i, fe in enumerate(graph_info.fe_list):
        # TODO - we assume fp32 atm.
        curr_size += fe.size * FP32_BYTES
        if curr_size >= bucket_size:
            stop = i + 1
            fe_list = graph_info.fe_list[start:stop]
            jit_buffer_node = _fuse_with_jit(graph_info, gm, fe_list)
            grad_nodes = _scatter_results_jit(
                graph_info, gm, fe_list, jit_buffer_node
            )

            _update_output_args(
                graph_info,
                gm,
                fe_list,
                new_output_args,
                grad_nodes,
            )

            # fusion done for this mb size
            curr_size = 0
            start = stop

    # fuse any leftovers - this is held over as an external step to try additional
    # splitting to minimize last all_reduce

    if start < len(graph_info.fe_list):
        fe_list = graph_info.fe_list[start:]
        jit_buffer_node = _fuse_with_jit(graph_info, gm, fe_list)
        grad_nodes = _scatter_results_jit(
            graph_info, gm, fe_list, jit_buffer_node
        )

        _update_output_args(
            graph_info,
            gm,
            fe_list,
            new_output_args,
            grad_nodes,
        )

    # update output with the buffer view args
    gm.graph.erase_node(graph_info.output)
    gm.graph.output(new_output_args)

    rebuild_graph(gm, remove_dead_code=True)
