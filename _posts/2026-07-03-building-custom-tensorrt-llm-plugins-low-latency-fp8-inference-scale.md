---
layout: post
title: "Building Custom TensorRT-LLM Plugins for Low-Latency FP8 Inference at Scale"
date: 2026-07-03 08:00:00 +0700
tags: [tensorrt-llm, fp8, cuda, llm-inference]
description: "A production-grade guide to writing custom TensorRT-LLM plugins in C++ and CUDA to accelerate low-latency FP8 inference."
image: "https://picsum.photos/seed/3696/1080/720"
thumbnail: "https://picsum.photos/seed/3696/400/300"
---

Serving large language models (LLMs) like LLaMA-3-70B or Mixtral-8x22B at scale requires aggressive quantization to keep hardware costs under control. NVIDIA’s Hopper architecture introduces native hardware support for FP8 (`E4M3` and `E5M2` formats), offering a theoretical 2x speedup in compute density over FP16/BF16. However, in production, raw compute speedups are frequently throttled by memory bandwidth limits and quantization overhead. In a typical transformer MLP block, computing a SwiGLU activation in BF16, writing the tensor back to High Bandwidth Memory (HBM), reading it back to scale and quantize it to FP8, and then writing it back to HBM for the subsequent matrix multiplication represents a massive memory bottleneck. On an H100 with 3.35 TB/s of memory bandwidth, these roundtrips devour latency. To achieve true line-rate inference, we must fuse the activation function and the FP8 quantization step into a single CUDA kernel. Because TensorRT-LLM is the leading orchestrator for Hopper serving, implementing this fusion requires writing a custom TensorRT C++ plugin. This post shows you how to design, write, compile, and integrate a custom vectorized SwiGLU activation + FP8 quantization plugin.

## The Cost of Non-Fused Quantization

To understand why fusion is critical, let's analyze the memory traffic of an unfused SwiGLU activation and quantization block. Consider a batch size of $N$ tokens and a hidden dimension $H$. The SwiGLU operation takes an input tensor $X \in \mathbb{R}^{N \times 2H}$ (in BF16, which is 2 bytes per element) and produces an output $Y \in \mathbb{R}^{N \times H}$.

In an unfused implementation:
1. The GPU reads $X$ from HBM: $2N \times 2H \times 2 \text{ bytes} = 8NH$ bytes.
2. The activation kernel runs, writing $Y$ to HBM in BF16: $N \times H \times 2 \text{ bytes} = 2NH$ bytes.
3. The quantization kernel reads $Y$ from HBM: $2NH$ bytes.
4. The quantization kernel scales and casts $Y$ to FP8, writing it back to HBM: $N \times H \times 1 \text{ byte} = NH$ bytes.

The total memory traffic is $13NH$ bytes. 

By fusing the SwiGLU activation and FP8 quantization into a single custom CUDA kernel:
1. The GPU reads $X$ from HBM: $8NH$ bytes.
2. The GPU performs the SwiGLU math in local registers, scales the result, casts it to FP8, and writes the output directly to HBM: $NH$ bytes.

The total memory traffic drops to $9NH$ bytes—a 30% reduction. On memory-bandwidth-bound generation phases (where batch sizes are small and memory transfers dominate latency), this reduction translates directly to a massive reduction in Time-to-First-Token (TTFT) and Inter-Token Latency (ITL).

## Anatomy of a TensorRT-LLM Plugin

To run our fused kernel inside a TensorRT-LLM execution graph, we must implement a custom C++ class that inherits from `nvinfer1::IPluginV2DynamicExt`. This interface tells the TensorRT builder how to configure our kernel, validate input/output dimensions, estimate workspace requirements, and serialize the plugin parameters into the compiled engine file.

The plugin lifecycle consists of two main phases:
1. **Compilation/Build Time:** TensorRT queries `getOutputDimensions` and `supportsFormatCombination` to build the execution graph. Once optimized, the builder calls `serialize` to save the plugin configuration into the compiled `.engine` file.
2. **Runtime serving:** The runtime deserializes the plugin using the `IPluginCreator` class, calls `initialize` to allocate resources, and invokes `enqueue` for every forward pass to execute the underlying CUDA kernel.

Let's begin by defining the class interface for our custom `FusedSwiGLUQuantPlugin`.

<script src="https://gist.github.com/mohashari/a3475555a1185f7f21ca101fe6cfd449.js?file=snippet-1.txt"></script>

## Fusing the Kernel in CUDA

With the interface defined, we must write a CUDA kernel optimized for the Hopper architecture. The target quantization format is FP8 `E4M3` (1 sign bit, 4 exponent bits, 3 mantissa bits). Hopper GPUs support natively converting floating-point values to FP8 via compiler intrinsics. We will use the CUDA `__nv_fp8_e4m3` datatype declared in `<cuda_fp8.h>`.

To achieve peak memory bandwidth, we vectorize the loads and stores:
* The input tensor $X$ is stored in BF16 format. Instead of loading single elements, we load 8 elements of BF16 at a time (16 bytes) using `float4` vector registers.
* The output tensor $Y$ is stored in FP8 format. Since 8 elements of FP8 require exactly 8 bytes, we can perform a vectorized 64-bit store using `double` or `uint2` casts. This prevents write-alignment stalls on the memory controller.

Here is the implementation of the fused kernel. It performs vectorized memory operations, computes the SwiGLU activation using high-precision floats internally, applies the quantization scaling factor, and saturates the outputs to the maximum representable value of FP8 `E4M3` ($\pm 448.0$).

<script src="https://gist.github.com/mohashari/a3475555a1185f7f21ca101fe6cfd449.js?file=snippet-2.txt"></script>

Next, write the C++ host wrapper function that sets up grid size, block dimensions, and dispatches the execution to the GPU.

<script src="https://gist.github.com/mohashari/a3475555a1185f7f21ca101fe6cfd449.js?file=snippet-3.txt"></script>

## Implementing the Plugin Interface in C++

Now we implement the class methods of our plugin. The key functions to implement correctly are `supportsFormatCombination` and `enqueue`. 

During engine building, TensorRT will query different data format combinations. Our plugin expects:
* Input 0 (Activations): BF16 or FP16 data type in linear layout.
* Input 1 (Quantization Scale): FP32 data type (scalar or 1D tensor).
* Output 0 (Quantized Output): FP8 (`DataType::kFP8` or `DataType::kFP8_E4M3`) in linear layout.

<script src="https://gist.github.com/mohashari/a3475555a1185f7f21ca101fe6cfd449.js?file=snippet-4.txt"></script>

Here we integrate the host-side launcher function within `enqueue()`. We calculate the logical token dimension $N$ dynamically by flattening all dimensions up to the last one. This layout ensures compatibility with variable-length sequence inputs (e.g., packed tensor layouts in TensorRT-LLM).

<script src="https://gist.github.com/mohashari/a3475555a1185f7f21ca101fe6cfd449.js?file=snippet-5.txt"></script>

## Serialization and the Plugin Creator

TensorRT-LLM decouples network design (which occurs offline) from inference deployment. During compilation, the builder serializes the plugin's internal settings (e.g., static scaling factors, layer properties) to a binary stream. The deployment environment deserializes this binary stream to instantiate the plugin. 

If your serialization and deserialization architectures mismatch, TensorRT will fail silently, leading to hard-to-trace segmentation faults or memory corruption at runtime.

We must implement serialization alongside the `PluginCreator` class responsible for registering our custom module under the TensorRT global registry.

<script src="https://gist.github.com/mohashari/a3475555a1185f7f21ca101fe6cfd449.js?file=snippet-6.txt"></script>

## Python Integration with TensorRT-LLM

To compile the plugin into your engine, compile the C++/CUDA code into a shared library (e.g., `libfused_swiglu_quant_plugin.so`). You must load this library in your Python compilation script using Python’s `ctypes` library before building the model. Loading the library triggers the static initialization constructor macro `REGISTER_TENSORRT_PLUGIN`, which registers the plugin in TensorRT’s runtime engine.

Once registered, you can reference the plugin within your model's computational graph using `tensorrt_llm.functional.custom_op`.

```python
# snippet-7
import ctypes
import os
import numpy as np
import tensorrt as trt
import tensorrt_llm
from tensorrt_llm.functional import Tensor, custom_op

def load_fused_swiglu_plugin():
    """
    Dynamically loads the compiled C++/CUDA plugin shared library.
    This registers the 'FusedSwiGLUQuant' creator in TensorRT's global registry.
    """
    plugin_path = "./libfused_swiglu_quant_plugin.so"
    if not os.path.exists(plugin_path):
        raise FileNotFoundError(f"Custom plugin library not found at: {plugin_path}")
    
    # Load shared library to trigger REGISTER_TENSORRT_PLUGIN
    ctypes.CDLL(plugin_path)

def fused_swiglu_quant(input_tensor: Tensor, scale_tensor: Tensor) -> Tensor:
    """
    Integrates the custom FusedSwiGLUQuant plugin inside a TensorRT-LLM network definition.
    
    Args:
        input_tensor (Tensor): Activation tensor of shape [N, 2 * H] in FP16 or BF16.
        scale_tensor (Tensor): Quantization scale factor of shape [1] in FP32.
    Returns:
        Tensor: Quantized FP8 activation of shape [N, H].
    """
    registry = trt.get_plugin_registry()
    plugin_creator = None
    
    # Search the active registry for the custom plugin
    for creator in registry.plugin_creator_list:
        if creator.name == "FusedSwiGLUQuant" and creator.plugin_version == "1":
            plugin_creator = creator
            break
            
    if plugin_creator is None:
        raise RuntimeError("FusedSwiGLUQuant plugin not registered. Did you call load_fused_swiglu_plugin()?")

    # Define the initialization fields for TensorRT plugin creator
    fc = trt.PluginFieldCollection([
        trt.PluginField("scale", np.array([1.0], dtype=np.float32), trt.PluginFieldType.FLOAT32),
        trt.PluginField("has_dynamic_scale", np.array([0], dtype=np.int32), trt.PluginFieldType.INT32)
    ])
    
    # Create an instance of the custom node
    plugin = plugin_creator.create_plugin("fused_swiglu_quant_node", fc)
    
    # Map the custom plugin instance inside the TensorRT-LLM functional graph
    # custom_op outputs a list of tensors; we extract output 0 (quantized activations)
    outputs = custom_op(plugin, [input_tensor, scale_tensor])
    return outputs[0]
```

## Production Pitfalls & Hard Lessons

Writing custom CUDA kernels and integrating them with TensorRT-LLM is straightforward in theory, but running them under production loads exposes several system-level failure modes.

### 1. Vector Alignment Violations
Our kernel uses 128-bit loads (`reinterpret_cast<const float4*>`) to maximize HBM cache-line efficiency. CUDA mandates that a `float4` address must be aligned to a 16-byte boundary. If the base address of your input tensor is misaligned, the GPU will raise an `Illegal Memory Access` error, instantly crashing your entire Triton Inference Server worker process.

In practice, TensorRT-LLM can slice or pad tensors during tensor parallelism (slicing weights across multiple GPUs). If the hidden size is sliced into a dimension that is not a multiple of 8 (e.g., hidden size of 4097 elements), or if the starting offset of a sequence token in a packed format does not align to 16 bytes, the vectorized load will fail. To make your code production-safe, always include a scalar fallback branch within the kernel to handle misaligned inputs:

<script src="https://gist.github.com/mohashari/a3475555a1185f7f21ca101fe6cfd449.js?file=snippet-8.txt"></script>

### 2. FP8 Saturation and Underflow
The `E4M3` format has a small dynamic range ($\pm 448$). If your static quantization scales are calibrated incorrectly, activations will saturate to $+448.0$ or $-448.0$. Alternatively, if activations are small, they will underflow to $0.0$.
* Saturation leads to catastrophic performance degradation (perplexity explosions, garbled model outputs).
* Underflow creates "dead activations," causing the LLM to output repetitive tokens.

To profile this in production, use NVIDIA Nsight Systems. Look for layers with unusually high percentages of maximum or zero values in their outputs. Implement dynamic scaling (calculating the max value per tensor in real-time) if your offline calibration fails to capture input variance during long-context conversations.

### 3. Thread-Safety in Triton and Concurrent Streams
When hosting your TensorRT-LLM engine on Triton Inference Server with concurrent model execution enabled, the same plugin instance can be executed concurrently on different CUDA streams by different worker threads. 

Your C++ plugin code must be entirely stateless during the `enqueue()` phase. Do not store intermediate tensors or runtime variables as member variables of the `FusedSwiGLUQuantPlugin` class. If the kernel requires temporary workspace memory (e.g., for block-level reductions during dynamic scale calculations), you must request this memory from the TensorRT engine builder by returning the required size from `getWorkspaceSize()`. TensorRT will safely allocate thread-safe memory and pass it to your kernel via the `workspace` pointer in `enqueue()`.