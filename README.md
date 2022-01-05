# PiPPy: Pipeline Parallelism for PyTorch

This project is an attempt to build a state-of-the-art automated Pipeline Parallelism system for PyTorch subject to the design considerations brought up in these [RFCs](https://github.com/pytorch/rfcs/pull/32). Some of the main design considerations include:

* An eye toward making running PyTorch code under Pipeline Parallelism as seamless as possible; that is, the user should have to make as few changes to their code as possible. In particular, we wish to elide the requirement of existing systems to structure your code as an `nn.Sequential`.
* First-class support for cross-host pipeline parallelism, as this is where PP is typically used (over slower interconnects)
* Composability with other parallelism schemes such as data parallelism or tensor splitting model parallelism
* Support for pipeline scheduling paradigms, including static schedules like fill-drain (GPipe), 1f1b, interleaved 1f1b and dynamic schedules like lookahead or registers/back-pressure.

# Design and Codebase Roadmap

## Program Capture and Intermediate Representation

`IR.py` defines the `Pipe` class, which is the main intermediate representation used in PiPPy. This intermediate representation consists of a restricted `fx.GraphModule`. In the top level `fx.Graph` representation, the IR is limited to only `placeholder` and `output` nodes, `call_module` nodes to call into the pipeline stages and `call_function` with a target of `operator.getitem`, for unpacking tuple outputs from pipeline stages. The top-level `fx.Graph` gives us 1) a topological ordering of pipeline stages and 2) the data dependencies between these pipeline stages.

We can create IR from existing PyTorch modules using one of several front-ends, exposed as static methods on `Pipe`. `Pipe.from_sequential` takes as argument an instance of `torch.nn.Sequential` and returns a `Pipe` instance that represents the trivial feed-forward nature of that sequential. For example:

```
mods = [torch.nn.Linear(512, 512) for _ in range(5)]
mods += [mods[0]]
seq = torch.nn.Sequential(*mods)

seq_pipe = Pipe.from_sequential(seq)

print(seq_pipe.split_gm)
"""
GraphModule(
  (0): Linear(in_features=512, out_features=512, bias=True)
  (1): Linear(in_features=512, out_features=512, bias=True)
  (2): Linear(in_features=512, out_features=512, bias=True)
  (3): Linear(in_features=512, out_features=512, bias=True)
  (4): Linear(in_features=512, out_features=512, bias=True)
  (5): Linear(in_features=512, out_features=512, bias=True)
)



def forward(self, input):
    input_1 = input
    _0 = getattr(self, "0")(input_1);  input_1 = None
    _1 = getattr(self, "1")(_0);  _0 = None
    _2 = getattr(self, "2")(_1);  _1 = None
    _3 = getattr(self, "3")(_2);  _2 = None
    _4 = getattr(self, "4")(_3);  _3 = None
    _5 = getattr(self, "5")(_4);  _4 = None
    return _5
"""
```

Similarly, we can use `Pipe.from_tracing` to use `torch.fx` tracing to convert an arbitrary `nn.Module` instance to this form. For example:

```
class ExampleCode(torch.nn.Module):
  def __init__(self):
    super().__init__()
    self.mm_param = torch.nn.Parameter(torch.randn(512, 512))
    self.mm_param2 = torch.nn.Parameter(torch.randn(512, 512))
    self.lin = torch.nn.Linear(512, 512)

  def forward(self, x):
    x = torch.mm(x, self.mm_param)
    skip_connection = x
    x = torch.relu(x)
    pipe_split()
    x = torch.mm(x, self.mm_param)
    x = self.lin(x)
    pipe_split()
    x = torch.relu(x)
    x = x + skip_connection
    x = torch.mm(x, self.mm_param2)
    x = self.lin(x)
    return x

ec = ExampleCode()
ec(torch.randn(50, 512))

ec_pipe = Pipe.from_tracing(ec, MultiUseParameterConfig.TRANSMIT)
print(ec_pipe.split_gm)
"""
GraphModule(
  (submod_0): GraphModule()
  (submod_1): GraphModule(
    (lin): Linear(in_features=512, out_features=512, bias=True)
  )
  (submod_2): GraphModule(
    (lin): Linear(in_features=512, out_features=512, bias=True)
  )
)



def forward(self, x):
    submod_0 = self.submod_0(x);  x = None
    getitem_2 = submod_0[2]
    getitem = submod_0[0]
    getitem_1 = submod_0[1];  submod_0 = None
    submod_1 = self.submod_1(getitem, getitem_2);  getitem = getitem_2 = None
    submod_2 = self.submod_2(submod_1, getitem_1);  submod_1 = getitem_1 = None
    return submod_2
"""
```

There are a few things to note about the above example:

1. We use `IR.pipe_split` to explicitly demarcate within the code where we want pipeline boundaries to be. `from_tracing` will collect all data dependencies across these calls to `pipe_split` and emit corresponding data dependencies in the pipeline graph.
2. Note the `skip_connection` value in the original program. `from_tracing` will correctly detect the usage of this value in non-adjacent pipeline stages and emit a connection in the top-level graph to forward this dependency from stage 0 to 2.
3. Notice that `self.mm_param` is used both in pipeline stage 0 and pipeline stage 1. Since we have specified `MultiUseParameterConfig.TRANSMIT` as the `multi_use_param_spec` argument to `from_tracing`, the system will emit code that will keep `mm_param` resident on stage 0 and transmit that value for use within stage 1. `multi_use_param_spec` can also be specified as a dictionary mapping parameter qualified names to a `MultiUseParameterConfig` value (one of `TRANSMIT` or `REPLICATE`) or it can be left as None to specify the default behavior (`TRANSMIT`) for all shared parameter. We will discuss replication in the following section.


Multi-use parameters can also be replicated. That is, each pipeline stage that uses a replicated parameter will have its own copy of the parameter and the system will record information about this replication such that the runtime can insert the proper synchronization operations upon update of these parameters. For example, let us rerun the above example with `multi_use_param_spec=MultiUseParameterConfig.REPLICATE`:

```
ec_pipe_replicated = Pipe.from_tracing(ec, MultiUseParameterConfig.REPLICATE)
print(ec_pipe_replicated.replicated_params)
"""
[{'submod_0': '__mm_param', 'submod_1': '__mm_param'},
 {'submod_1': 'lin.weight', 'submod_2': 'lin.weight'},
 {'submod_1': 'lin.bias', 'submod_2': 'lin.bias'}]
"""
```

Note that the `Pipe` instance has an attribute `replicated_params`, which is a record of all of the parameters that are replicated across pipeline stages. This object is a list of dictionaries. Each dictionary represents a single value that has been replicated across stages. The keys of the dictionary are the qualified name of the pipeline stage submodules that hold copies of this parameter, and the values are the qualified name of the parameter itself within those pipeline stage modules. Note that not only do we see `mm_param` in the above example, but we also see parameter replication from the usage of the `self.lin` module in multiple pipeline stages. `self.lin` is a "leaf module" in `torch.fx` parlance, and since we cannot see into the implementation of a leaf module, we automatically replicate leaf module parameters (i.e. they cannot be transmitted).

## Runtime

TODO

# Open questions

TODO
