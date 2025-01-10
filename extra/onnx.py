from typing import Callable, Any, Sequence
import importlib, functools
import numpy as np
from tinygrad import Tensor, dtypes
from tinygrad.helpers import getenv, DEBUG, all_same, get_single_element
from tinygrad.dtype import DType, ConstType
from tinygrad.device import is_dtype_supported
from onnx import AttributeProto, ModelProto, TensorProto, ValueInfoProto, helper
from google.protobuf.json_format import MessageToDict

cache_misses = 0
@functools.lru_cache(None)
def _cached_to_python_const(t:Tensor):
  if t.dtype is dtypes.uint8: return t.data().tobytes()
  if 0 in t.shape: return []
  return t.tolist()

# Tensor -> python value cache for parameters
def to_python_const(t) -> list[ConstType]|ConstType|bytes:
  if not isinstance(t, Tensor): return t
  global cache_misses
  ret = _cached_to_python_const(t)
  if (info := _cached_to_python_const.cache_info()).misses > cache_misses and DEBUG >= 3:
    print(f"Cache miss for {t}")
    cache_misses = info.misses
  return ret

# TODO: use real float16
# src: onnx/mapping.py
DTYPE_MAP: dict[int, DType] = {
  TensorProto.FLOAT:dtypes.float32, TensorProto.UINT8:dtypes.uint8, TensorProto.INT8:dtypes.int8,
  TensorProto.UINT16:dtypes.uint16, TensorProto.INT16:dtypes.int16, TensorProto.INT32:dtypes.int32, TensorProto.INT64:dtypes.int64,
  TensorProto.BOOL:dtypes.bool, TensorProto.FLOAT16:dtypes.float32, TensorProto.DOUBLE:dtypes.double, TensorProto.UINT32:dtypes.uint32,
  TensorProto.UINT64:dtypes.uint64, TensorProto.BFLOAT16:dtypes.bfloat16,
}
def dtype_parse(onnx_dtype: int) -> DType:
  if onnx_dtype not in DTYPE_MAP: raise NotImplementedError(f"onnx dtype {TensorProto.DataType.Name(onnx_dtype)} is not supported")
  return DTYPE_MAP[onnx_dtype] if is_dtype_supported(DTYPE_MAP[onnx_dtype]) else dtypes.float

# src: onnx/onnx_ml_pb2.pyi
ATTRIBUTE_MAP: dict[AttributeProto.AttributeType, Callable[[AttributeProto], Any]] = {
  AttributeProto.FLOAT: lambda a: float(a.f), AttributeProto.INT: lambda a: int(a.i),
  AttributeProto.STRING: lambda a: a.s.decode("utf-8"), AttributeProto.TENSOR: lambda a: buffer_parse(a.t),
  AttributeProto.FLOATS: lambda a: tuple(float(x) for x in a.floats), AttributeProto.INTS: lambda a: tuple(int(x) for x in a.ints),
  AttributeProto.STRINGS: lambda a: tuple(x.decode("utf-8") for x in a.strings)
}
def attribute_parse(onnx_attribute: AttributeProto):
  if onnx_attribute.type not in ATTRIBUTE_MAP:
    raise NotImplementedError(f"attribute with type {AttributeProto.AttributeType.Name(onnx_attribute.type)} is not supported")
  return ATTRIBUTE_MAP[onnx_attribute.type](onnx_attribute)

def buffer_parse(onnx_tensor: TensorProto) -> Tensor:
  if onnx_tensor.string_data: raise NotImplementedError("Parsing for buffer with string data is not implemented.")
  dtype, shape = dtype_parse(onnx_tensor.data_type), tuple(onnx_tensor.dims)
  if data := list(onnx_tensor.float_data) or list(onnx_tensor.int32_data) or list(onnx_tensor.int64_data) or list(onnx_tensor.double_data) or \
             list(onnx_tensor.uint64_data):
    if shape == (): return Tensor(get_single_element(data), dtype=dtype).reshape(shape).realize()
    return Tensor(data, dtype=dtype).reshape(shape).realize()
  assert onnx_tensor.HasField("raw_data")
  return Tensor(np.frombuffer(onnx_tensor.raw_data, dtype=helper.tensor_dtype_to_np_dtype(onnx_tensor.data_type)).copy().reshape(shape), dtype=dtype)

onnx_ops = importlib.import_module('extra.onnx_ops')
ONNXLIMIT = getenv("ONNXLIMIT", -1)
def get_run_onnx(onnx_model: ModelProto):
  # model initialization data
  model_tensors = {inp.name:buffer_parse(inp) for inp in onnx_model.graph.initializer}
  model_expected_inputs = {inp.name:inp for inp in onnx_model.graph.input if inp.name not in model_tensors}
  model_attributes = {num:{x.name:attribute_parse(x) for x in n.attribute} for num,n in enumerate(onnx_model.graph.node)}

  # model descriptions
  # TODO: need a better way of controlling training vs non-training
  is_onnx_preview_training = any(n.HasField("domain") and n.domain == "ai.onnx.preview.training" for n in onnx_model.graph.node)
  onnx_model_version = onnx_model.opset_import[0].version

  # used to check validity of user_input according to their dimension variables
  variable_dims = {}

  # mapping from onnx ops to tensor.py ops
  tensor_methods = {
    op:op.lower() for op in ("Neg", "Reciprocal", "Pow", "Sqrt", "Sign", "Abs", "Exp", "Log", "Mish", "Sin", "Cos", "Tan", "Asin", "Acos", "Atan",
    "Relu", "Sigmoid", "MatMul", "Floor", "Ceil", "IsInf", "IsNaN", "Softplus", "HardSwish", "Where", "Mul", "Sinh", "Cosh", "Tanh",
    "Softsign", "Asinh", "Acosh", "Atanh",  "Elu", "Celu", "Selu", "Xor", "Round", "Erf", "Mod")
  }

  # these values are expected to be python consts
  required_input_python_consts: dict[str, tuple[int, ...]] = {
    "Tile": (1,), "Range": (0,1,2), "Expand": (1,), "Reshape": (1,), "Squeeze": (1,), "Unsqueeze": (1,), "Trilu": (1,), "ConstantOfShape": (0,),
    "CumSum": (1,), "Pad": (1,2,3), "MaxUnpool": (2,), "Dropout": (1,2), "CenterCropPad": (1,), "OneHot": (1,), "Compress": (1,),
    "ImageDecoder": (0,), "AffineGrid": (1,), "Resize": (1,2,3), "Upsample": (1,), "Split": (1,), "Slice": (1,2,3,4),
    **{"Reduce"+r: (1,) for r in ("Max", "Min", "Sum", "Mean", "SumSquare", "Prod", "L1", "L2", "LogSum", "LogSumExp")},
    **{optim: (1,) for optim in ("Adam", "Adagrad", "Momentum")}
  }

  # src: https://onnx.ai/onnx/repo-docs/IR.html#input-output-data-types
  # parses and validates inputs based on their shape and dtype specified by model
  def prepare_input(user_input:Any, model_input:ValueInfoProto):
    type_proto = model_input.type
    if type_proto.HasField("optional_type"):
      if user_input is None: return Tensor(None)
      type_proto = type_proto.optional_type.elem_type
    if type_proto.HasField("sequence_type"):
      if not isinstance(user_input, Sequence): raise RuntimeError(f"{model_input.name} received {user_input}, expected sequence type")
      dtype = dtype_parse(type_proto.sequence_type.elem_type.tensor_type.elem_type)
      sequence = [Tensor(i, dtype=dtype, requires_grad=is_onnx_preview_training) if not isinstance(i, Tensor) else i for i in user_input]
      if not all_same(tuple(t.shape for t in sequence)): raise RuntimeError(f"shapes for {model_input.name} must be homogeneous")
      # TODO: need true float16 for dtype checking
      # if not all(t.dtype is dtype for t in sequence):
      #   raise RuntimeError(f"{model_input.name} has dtype mismatch for sequence type. Expected {dtype}, received {tensor.dtype}.")
      return sequence
    if type_proto.HasField("tensor_type"):
      dtype = dtype_parse(type_proto.tensor_type.elem_type)
      tensor = Tensor(user_input, dtype=dtype, requires_grad=is_onnx_preview_training) if not isinstance(user_input, Tensor) else user_input
      # TODO: need true float16 for dtype checking
      # if dtype is not tensor.dtype: raise RuntimeError(f"{model_input.name} has mismatch for dtype. Expected {dtype}, received {tensor.dtype}.")
      for dim, onnx_dim in enumerate(type_proto.tensor_type.shape.dim):
        dim_param, dim_value = onnx_dim.dim_param, onnx_dim.dim_value
        user_dim_input = user_input.shape[dim]
        if dim_param: dim_value = variable_dims[dim_param] if dim_param in variable_dims else variable_dims.setdefault(dim_param, user_dim_input)
        if user_dim_input != dim_value:
          raise RuntimeError(f"{model_input.name} has mismatch for dim={dim_param or dim}. Expected {dim_value}, received {user_dim_input}.")
      return tensor
    type_field_names = [field.name for field,_ in type_proto.ListFields()]
    raise NotImplementedError(f"{model_input.name} with {type_field_names=} is not supported")

  def run_onnx(inputs={}, debug=0):
    debug = getenv("DEBUGONNX") or debug
    if debug >= 3: print("Model initialization data:\n" + "\n".join(f"\t{i.name} - {model_tensors[i.name]}" for i in onnx_model.graph.initializer))

    if debug >= 1: print("Model input:")
    for name, value_info in model_expected_inputs.items():
      if name not in inputs: raise RuntimeError(f"Please provide input data for {name}")
      model_tensors[name] = prepare_input(inputs[name], value_info)
      if debug >= 1: print(f"\t{name} - {model_tensors[name]}")
      if debug >= 2: print(f"\t\t{MessageToDict(value_info.type)}")

    for num,n in enumerate(onnx_model.graph.node):
      inp_tensors = [model_tensors.get(x) for x in n.input]
      required_consts = required_input_python_consts.get(n.op_type, ())
      inp = [to_python_const(t) if i in required_consts else t for i,t in enumerate(inp_tensors)]
      opt = model_attributes[num]

      if debug >= 1: print(f"{num}: op \"{n.op_type}\" input shapes {[x.shape if isinstance(x, Tensor) else x for x in inp_tensors]} opt {opt}")
      if debug >= 3:
        print("\tinputs:")
        print("\n".join(f"\t\t{x} - {t!r}" + (" (to_python_const)" if i in required_consts else "") for i,(x,t) in enumerate(zip(n.input, inp))))

      # provide additional arguments
      if n.op_type == "Split" and 'num_outputs' not in opt: opt['num_outputs'] = len(n.output)
      if n.op_type == "Gradient": opt['intermediate_tensors'] = model_tensors

      # run op
      if n.op_type in tensor_methods: ret = getattr(Tensor, tensor_methods[n.op_type])(*inp, **opt)
      elif hasattr(onnx_ops, n.op_type):
        fxn = getattr(onnx_ops, n.op_type)
        if isinstance(fxn, dict):
          for k in sorted(fxn.keys()):
            if k <= onnx_model_version:
              real_fxn = fxn[k]
        else:
          real_fxn = fxn
        ret = real_fxn(*inp, **opt)
      else:
        print("UNSUPPORTED", n.op_type, n.input, n.output)
        raise NotImplementedError(f"op_type {n.op_type} not supported")

      # finalization after running the op
      if not isinstance(ret, tuple): ret = (ret, )
      if len(n.output) > len(ret): raise RuntimeError(f"expected output size must be less than {len(ret)}, it's {n.output}")
      for i in range(len(n.output)): model_tensors[n.output[i]] = ret[i]
      if debug >= 2: print("\toutputs:\n" + "\n".join(f"\t\t{n.output[i]} - {ret[i]}" for i in range(len(n.output))))

      if num == ONNXLIMIT: return {name:model_tensors[name] for name in n.output}
    return {x.name:model_tensors[x.name] for x in onnx_model.graph.output}
  return run_onnx
