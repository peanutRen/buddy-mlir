# ===- frontend.py -------------------------------------------------------------
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# ===---------------------------------------------------------------------------
#
# This is the entry of the Buddy Compiler frontend.
#
# ===---------------------------------------------------------------------------

import operator
from typing import Any, List, Union, Optional
import functools

import mlir.dialects.func as func
import mlir.ir as ir
import torch
import torch._dynamo as dynamo
from torch._functorch.aot_autograd import aot_module_simplified
import torch.utils._pytree as pytree

from .ops.math import ops_registry as math_ops_registry
from .ops.tosa import ops_registry as tosa_ops_registry
from .ops.linalg import ops_registry as linalg_ops_registry


class DynamoCompiler:
    """
    Dynamo Compiler is one of the frontends of Buddy Compiler.
    Dynamo Compiler acts as a custom compiler for the Torch Dynamo framework,
    which converts an FX Graph into an equivalent MLIR module.

    Attributes:
        imported_module: The imported MLIR module after compilation.
        imported_params: The imported parameters from the model.
    """

    def __init__(
        self,
        func_name: str = "forward",
        primary_registry: dict = {},
        aot_autograd_decomposition: Optional[dict] = None,
        param_pack: bool = True,
        is_inference: bool = False,
    ) -> None:
        """
        Initializes the Dynamo Compiler.

        Args:
            func_name (str, optional): The function name to be used.
            primary_registry (dict, optional): The primary operations registry.
            aot_autograd_decomposition (Optional[dict], optional):
                The ahead-of-time autograd decomposition dictionary.
        """
        self._func_name = func_name
        self._aot_autograd_decomposition = aot_autograd_decomposition
        self._imported_module = None
        self._imported_params = None
        self._param_pack = param_pack
        self._is_inference = is_inference
        self._ops_registry = {}
        self._ops_registry.update(math_ops_registry)
        self._ops_registry.update(linalg_ops_registry)
        self._ops_registry.update(tosa_ops_registry)
        self._ops_registry.update(primary_registry)

    @property
    def imported_module(self):
        """Returns the imported MLIR module after compilation."""
        return self._imported_module

    @property
    def imported_params(self):
        """Returns the imported parameters from the model."""
        return self._imported_params

    def _compile_fx(
        self, gm: torch.fx.GraphModule, inputs: List[torch.Tensor]
    ) -> Any:
        """
        Compiles the provided FX Graph to MLIR module.

        Args:
            gm (torch.fx.GraphModule): The GraphModule to be compiled.
            inputs (List[torch.Tensor]): The input tensors.

        Returns:
            Any: The result of the ahead-of-time compiled module.
        """

        def _compiler(_gm: torch.fx.GraphModule, _inputs: List[torch.Tensor]):
            """Compile a FX graph in Aten/Prims IR to MLIR."""
            func_params = _inputs[: len(self.imported_params)]
            func_inputs = _inputs[len(self.imported_params) :]

            # Initializes the MLIR context.
            ctx = ir.Context()
            with ir.Location.unknown(ctx):
                fx_importer = FXGraphImporter(
                    _gm,
                    func_params,
                    func_inputs,
                    self._param_pack,
                    self._func_name,
                    self._ops_registry,
                    self._is_inference,
                )
                self._imported_module = fx_importer.import_graph()
            # TODO: Lower to LLVM dialect and use JIT engine to execute.
            return _gm.forward

        params = {
            **dict(gm.named_parameters(remove_duplicate=False)),
            **dict(gm.named_buffers(remove_duplicate=False)),
        }
        params_flat, params_spec = pytree.tree_flatten(params)
        self._imported_params = params_flat

        return aot_module_simplified(
            gm,
            inputs,
            fw_compiler=_compiler,
            decompositions=self._aot_autograd_decomposition,
        )

    def __call__(
        self, gm: torch.fx.GraphModule, inputs: List[torch.Tensor]
    ) -> Any:
        """
        A callable method that wraps around the `_compile_fx` method.

        Args:
            gm (torch.fx.GraphModule): The GraphModule to be compiled.
            inputs (List[torch.Tensor]): The input tensors.

        Returns:
            Any: The result of the ahead-of-time compiled module.
        """
        return self._compile_fx(gm, inputs)

    def importer(self, model, *args, **kwargs):
        """
        Imports the provided model as MLIR module and flat parameters.

        Args:
            model: The model to be imported.
            args: Arguments for the model.
            kwargs: Keyword arguments for the model.

        Returns:
            module: The imported MLIR module.
            params: The imported flat parameters.
        """
        model_opt = dynamo.optimize(self._compile_fx)(model)
        model_opt(*args, **kwargs)
        module = self._imported_module
        params = self._imported_params
        return module, params


class FXGraphImporter:
    """
    Imports an FX graph and generates an MLIR module in high-level dialects.

    Attributes:
        _symbol_table (dict): A dictionary to keep track of the symbols.
        _gm (torch.fx.GraphModule): The FX graph module to be imported.
        _func_name (str): Name of the generated MLIR function.
        _inputs (List[torch.Tensor]): Input tensor(s) of the FX graph.
        _num_input_visited (int): Number of input nodes that have been visited.
        _module (mlir.ir.Module): The generated MLIR module.
        _ops_registry (dict): Registry for the candidate operations.
    """

    def __init__(
        self,
        gm: torch.fx.GraphModule,
        params: List[torch.Tensor],
        inputs: List[torch.Tensor],
        param_pack: bool = True,
        func_name: str = "forward",
        ops_registry: dict = {},
        is_inference: bool = False,
    ):
        """
        Initializes the FX Graph importer.

        Note: If is_inference is True, the return tensors num will be forced to
        limit to one, such as returns = [returns[0]]

        Args:
            gm (torch.fx.GraphModule): The FX graph that will be imported.
            inputs (List[torch.Tensor]): Input tensor(s) of the FX graph.
            func_name (str): Name of the generated MLIR function.
            ops_registry (dict): Registry for the candidate operations.
        """
        self._symbol_table = {}
        self._gm = gm
        self._func_name = func_name
        self._params = params
        self._inputs = inputs
        self._param_pack = param_pack
        self._num_input_visited = 0
        self._module = ir.Module.create()
        self._ops_registry = ops_registry
        self._is_inference = is_inference

    def _torch_dtype_to_mlir_dtype(self, dtype: torch.dtype) -> ir.Type:
        """
        Converts a torch dtype to the corresponding MLIR dtype.

        Args:
            dtype (torch.dtype): The torch data type.

        Returns:
            mlir.ir.Type: The corresponding MLIR data type.

        Raises:
            NotImplementedError: If the given dtype is not supported.
        """
        match dtype:
            case torch.int32:
                return ir.IntegerType.get_signless(32)
            case torch.int64:
                return ir.IntegerType.get_signless(64)
            case torch.float32:
                return ir.F32Type.get()
            case torch.bool:
                return ir.IntegerType.get_signless(1)
            case _:
                raise NotImplementedError(f"Unsupported dtype {dtype}")

    def import_graph(self) -> ir.Module:
        """
        Imports FX graph and generates an MLIR module in high-level dialects.

        Returns:
            mlir.ir.Module: An MLIR module in high-level dialects.
        """
        with ir.InsertionPoint(self._module.body):
            arguments = []
            if self._param_pack:
                tensor_size = 0
                for param in self._params:
                    tensor_size += functools.reduce(
                        lambda x, y: x * y, list(param.shape)
                    )
                self._params_size = tensor_size
                self._offset = 0
                if tensor_size != 0:
                    dtype = self._torch_dtype_to_mlir_dtype(
                        self._params[0].dtype
                    )
                    tensor_arg = ir.RankedTensorType.get([tensor_size], dtype)
                    arguments.append(tensor_arg)
                inputs = self._inputs
            else:
                inputs = self._params + self._inputs
            for arg in inputs:
                shape_list = list(arg.shape)
                torch_dtype = arg.dtype
                mlir_dtype = self._torch_dtype_to_mlir_dtype(torch_dtype)
                tensor_arg = ir.RankedTensorType.get(shape_list, mlir_dtype)
                arguments.append(tensor_arg)

            @func.FuncOp.from_py_func(*arguments, name=self._func_name)
            def generated_func(*args):
                args_list = list(args)
                for node in self._gm.graph.nodes:
                    if node.op == "output":
                        output_node_args = node.args[0]
                        returns = []
                        for output_arg in output_node_args:
                            op = self._symbol_table.get((str(output_arg), 0))
                            returns.append(op)
                        if self._is_inference:
                            returns = [returns[0]]
                        self._symbol_table[("output", 0)] = returns
                    elif node.op == "placeholder":
                        self._import_placeholder(node, args_list)
                    else:
                        if node.target is operator.getitem:
                            self._symbol_table[
                                (str(node.name), 0)
                            ] = self._symbol_table[
                                (str(node.args[0]), node.args[1])
                            ]
                        else:
                            self._import_op(node)

                return self._symbol_table.get(("output", 0))

        return self._module

    def _import_placeholder(self, node: torch.fx.Node, args_list):
        """
        Imports a placeholder node from the FX graph.

        Args:
            node (torch.fx.Node): The FX node representing the placeholder.
            args_list (List[torch.Tensor]): List of input tensors.
        """
        if self._num_input_visited < len(self._params):
            placeholder_name = self._ops_registry["param.extract"](
                node, self._offset, args_list[0]
            ).result
            self._offset += functools.reduce(
                lambda x, y: x * y, list(node.meta["tensor_meta"].shape)
            )
        else:
            if len(self._params) > 0:
                placeholder_name = args_list[
                    self._num_input_visited - len(self._params) + 1
                ]
            else:
                placeholder_name = args_list[self._num_input_visited]
        self._symbol_table[(str(node.name), 0)] = placeholder_name
        self._num_input_visited += 1

    def _import_op(self, node: torch.fx.Node):
        """
        Imports an operation node from the FX graph.

        Args:
            node (torch.fx.Node): The FX node representing the operation.

        Raises:
            ValueError: If the node target doesn't have a __name__ attribute.
        """
        op_name = getattr(node.target, "__name__", None)
        if op_name is None:
            raise ValueError("node.target does not have a __name__ attribute")
        op_ret: ir.Operation | ir.Value | tuple | ir.OpResult = (
            self._ops_registry[op_name](node, self._symbol_table)
        )
        if isinstance(op_ret, tuple):
            for i, operation in enumerate(op_ret):
                self._symbol_table[(str(node.name), i)] = operation.result
        elif isinstance(op_ret, ir.OpResult):
            self._symbol_table[(str(node.name), 0)] = op_ret
        else:
            self._symbol_table[(str(node.name), 0)] = op_ret.result
