# ===- import-llama2.py --------------------------------------------------------
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
# This is the test of llama2 model.
#
# ===---------------------------------------------------------------------------

import os

import numpy
import torch
from transformers import LlamaForCausalLM, LlamaTokenizer
import torch._dynamo as dynamo
from torch._inductor.decomposition import decompositions as inductor_decomp
from torch._functorch.aot_autograd import aot_autograd_decompositions

from buddy.compiler.frontend import DynamoCompiler
from buddy.compiler.ops import tosa


# Retrieve the LLaMA model path from environment variables.
model_path = os.environ.get("LLAMA_MODEL_PATH")
if model_path is None:
    raise EnvironmentError(
        "The environment variable 'LLAMA_MODEL_PATH' is not set or is invalid."
    )

# Initialize the tokenizer and model from the specified model path.
tokenizer = LlamaTokenizer.from_pretrained(model_path)
model = LlamaForCausalLM.from_pretrained(model_path, torchscript=True)

# Initialize Dynamo Compiler with specific configurations as an importer.
dynamo_compiler = DynamoCompiler(
    primary_registry=tosa.ops_registry,
    aot_autograd_decomposition=aot_autograd_decompositions,
    is_inference=True,
)

# Import the model into MLIR module and parameters.
gm, params = dynamo_compiler.importer(
    model, torch.tensor([[1 for i in range(80)]], dtype=torch.int64)
)

# Write the MLIR module to the file.
with open(
    os.path.dirname(os.path.abspath(__file__)) + "/llama.mlir", "w"
) as module_file:
    print(gm, file=module_file)

# Concatenate all parameters into a single numpy array and write to a file.
all_param = numpy.concatenate(
    [param.detach().numpy().reshape([-1]) for param in params]
)
all_param.tofile(os.path.dirname(os.path.abspath(__file__)) + "/arg0.data")
