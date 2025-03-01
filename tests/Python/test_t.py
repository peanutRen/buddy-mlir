# RUN: %PYTHON %s 2>&1 | FileCheck %s

import torch
import torch._dynamo as dynamo
from torch._inductor.decomposition import decompositions as inductor_decomp
from torch._functorch.aot_autograd import aot_autograd_decompositions

from buddy.compiler.frontend import DynamoCompiler
from buddy.compiler.ops import tosa


def foo(x):
    return x.t()


in1 = torch.ones([13, 13], dtype=torch.float32)
# Initialize the dynamo compiler.
dynamo_compiler = DynamoCompiler(
    primary_registry=tosa.ops_registry,
    aot_autograd_decomposition=aot_autograd_decompositions,
)

foo_mlir = dynamo.optimize(dynamo_compiler)(foo)
foo_mlir(in1)

# CHECK: module {
# CHECK-LABEL: func.func @forward
# CHECK: %{{.*}} = "tosa.const"
# CHECK: %{{.*}} = "tosa.transpose"
# CHECK: return %{{.*}}
# CHECK: }
# CHECK: }
print(dynamo_compiler.imported_module)
