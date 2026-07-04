---
layout: post
title: "Fine-Tuning LoRA Adapters on Kubernetes: Orchestrating Distributed Training with Ray and Ludwig"
date: 2026-07-04 08:00:00 +0700
tags: [kubernetes, ray, machine-learning, lora, llmops]
description: "A production-grade guide to orchestrating distributed LoRA fine-tuning on Kubernetes using Ray, Ludwig, and DeepSpeed."
image: "https://picsum.photos/seed/4407/1080/720"
thumbnail: "https://picsum.photos/seed/4407/400/300"
---

In production, fine-tuning large language models (LLMs) like Llama-3-8B or Mistral-7B can quickly turn into a financial and operational nightmare. Standard full-parameter fine-tuning demands massive GPU clusters, leading to astronomical cloud bills and high infrastructure complexity. While Parameter-Efficient Fine-Tuning (PEFT) through Low-Rank Adaptation (LoRA) reduces trainable parameter counts by 99%—minimizing memory requirements and enabling smaller groups of GPUs to run training—executing this in a distributed fashion in production introduces a new set of challenges. Engineers must coordinate data pipelines, orchestrate distributed compute nodes, handle GPU memory spikes (such as PyTorch Out-of-Memory crashes), and ensure high network throughput for gradient synchronization. On Kubernetes, setting up raw PyTorch DistributedDataParallel (DDP) jobs manually requires tedious configuration of head/worker pods, environment variables, and storage mounts. This guide outlines how to build a robust, scalable, and fully orchestrated distributed fine-tuning pipeline on Kubernetes by leveraging Ray (using the KubeRay operator) and Ludwig (as a declarative machine learning framework), backed by DeepSpeed ZeRO-3.

## Architectural Architecture: Why Ray and Ludwig?

Kubernetes is the gold standard for container orchestration, but it was not natively designed for high-performance computing (HPC) workloads like distributed deep learning. To bridge this gap, we use **KubeRay**, a Kubernetes operator that manages Ray clusters. KubeRay handles the provisioning of Ray head and worker pods, setting up network links, and dynamic scaling.

Inside the Ray cluster, **Ray Train** manages the distributed execution loop, exposing an interface compatible with standard deep learning frameworks. Instead of writing boilerplate PyTorch DDP code, we use **Ludwig**, an open-source declarative machine learning framework. Ludwig abstracts the training logic into a simple YAML configuration file. Under the hood, Ludwig handles:
- Distributed data loading and preprocessing (using Ray Data).
- Tokenization on CPU workers.
- Model instantiation and weight initialization.
- PyTorch DDP orchestration with DeepSpeed.
- Metric tracking and exporting checkpoints to object storage.

By combining Ray and Ludwig, backend engineers can scale LLM fine-tuning across multiple nodes and GPUs without writing complex infrastructure code or low-level PyTorch code.

## Provisioning the Ray Cluster on Kubernetes

Before running training, we need to deploy a Ray cluster. The KubeRay operator uses a custom resource definition (CRD) called `RayCluster`. When configuring the worker nodes, there are several production-critical details to address:

1. **Shared Memory (`/dev/shm`)**: PyTorch's DDP and DataLoader processes use shared memory for inter-process communication. The default Docker shared memory size is 64MB, which causes instant OOM crashes during training. We must mount an `emptyDir` volume backed by memory (`Memory` medium) to `/dev/shm`.
2. **Node Affinity and Tolerations**: Worker pods must run on GPU-enabled nodes, while the head pod can run on cheaper CPU-only nodes.
3. **Resource Requests and Limits**: We must specify exact requests and limits for memory, CPU, and GPUs. For production, set CPU and memory requests equal to limits to avoid resource throttling and eviction.

The following manifest defines a production-ready KubeRay cluster with a head node and a GPU worker pool:

<script src="https://gist.github.com/mohashari/48285f1412aa626dd4f2259b3c7ec0be.js?file=snippet-1.yaml"></script>

## Declarative Fine-Tuning: The Ludwig Configuration

With our infrastructure defined, we now configure the training job using Ludwig's declarative configuration. This file defines the LLM model structure, the dataset input and output columns, the LoRA hyperparameters, the DeepSpeed strategy, and the Ray backend resources.

When configuring LoRA:
- **`r` (Rank)**: Controls the size of the low-rank matrices. A rank of `8` or `16` is standard. Higher values increase parameters and GPU memory requirements but allow the model to learn more complex features.
- **`alpha`**: The scaling factor for the LoRA adapter. Usually set to double the rank (`2 * r`).
- **`target_modules`**: The specific projection layers to inject the adapter matrices. Targeting `q_proj`, `k_proj`, `v_proj`, and `o_proj` provides the best balance between performance and training stability.

For memory optimization, we integrate DeepSpeed ZeRO-3, which offloads the model states and parameters to CPU memory when they are not actively being updated.

<script src="https://gist.github.com/mohashari/48285f1412aa626dd4f2259b3c7ec0be.js?file=snippet-2.yaml"></script>

## Submitting and Executing the Job

To run the training pipeline, we use the Ray Job Submission API. This allows developers to submit training jobs programmatically without manually executing scripts inside container terminals. It also makes it easier to integrate our machine learning pipeline with CI/CD tools or workflow engines like Apache Airflow or Prefect.

We need two scripts: a client script to submit the job, and a training script that executes inside the Ray cluster.

First, here is the training entrypoint script (`train.py`) that will run inside the cluster:

<script src="https://gist.github.com/mohashari/48285f1412aa626dd4f2259b3c7ec0be.js?file=snippet-3.py"></script>

Next, here is the Python script (`submit_job.py`) running locally or in a deployment pipeline to submit the job to the cluster:

<script src="https://gist.github.com/mohashari/48285f1412aa626dd4f2259b3c7ec0be.js?file=snippet-4.py"></script>

## Hard-Earned Production Failure Modes and Mitigations

Running distributed machine learning in production exposes infrastructure issues that simple test runs ignore. Below are the three most common failure modes when training LoRA adapters with Ray, along with mitigations.

### Failure Mode 1: NCCL Timeouts and Under-the-Hood Network Bottlenecks

**Symptom**: The training process starts, logs model parameters, and then freezes indefinitely on the first epoch. Eventually, the job fails with a message like: `watchdog: NCCL watchdog thread detected socket accept timeout`.

**Root Cause**: PyTorch uses Nvidia Collective Communications Library (NCCL) for GPU-to-GPU data synchronization. In a Kubernetes environment, NCCL can struggle to identify the correct network interface. For example, if workers have multiple interfaces (e.g., flannel, calico, or local loops), NCCL might try to route traffic through the wrong loopback device. Additionally, MTU mismatches can cause packet drops.

**Mitigation**: Set the `NCCL_SOCKET_IFNAME` environment variable in your runtime configuration to force NCCL to use the correct network interface (typically `eth0`). You should also disable InfiniBand support if your nodes do not have it, preventing NCCL from searching for hardware drivers that aren't there.

```python
# Enable inside your job environment variables:
"env_vars": {
    "NCCL_DEBUG": "INFO",
    "NCCL_IB_DISABLE": "1",
    "NCCL_SOCKET_IFNAME": "eth0"
}
```

### Failure Mode 2: Host Memory OOM due to Ray Object Store Spilling

**Symptom**: Worker pods are terminated by the Kubernetes OOMKiller with `Exit Code 137`. System logs show standard memory usage was normal, and GPU memory was not exhausted.

**Root Cause**: Ray utilizes a shared-memory object store called Plasma to pass datasets between tasks (such as tokenized chunks sent from CPUs to GPU processes). If `object_store_memory` is not configured, Ray defaults to allocating up to 30% of the worker node's total RAM. Under heavy data loading, this limit is easily reached, causing Ray to write data to disk. The write operations can cause system memory spikes that trigger the Kubernetes OOMKiller.

**Mitigation**: Restrict Ray's object store memory to a fixed allocation in the `rayStartParams` of your worker node configurations. Setting it to 30-40% of physical worker node memory leaves enough head-room for deep learning framework tasks.

```yaml
# Add in your workerGroupSpecs:
rayStartParams:
  object-store-memory: "16000000000" # Explicitly limit to ~16GB of host RAM
```

### Failure Mode 3: DeepSpeed ZeRO-3 GPU Memory Leaks and OOMs

**Symptom**: Training fails with `CUDA out of memory` during the backward pass of the first batch, even when using LoRA and a batch size of 1.

**Root Cause**: DeepSpeed ZeRO-3 partitions optimizer states, gradients, and model parameters across workers. However, gradient accumulation and intermediate activation tensors can still exceed available GPU VRAM. If your training config does not leverage CPU offloading or activation checkpointing, the GPU memory will run out.

**Mitigation**: Configure DeepSpeed to offload the optimizer states and parameters to CPU memory, and enable activation checkpointing inside the Ludwig DeepSpeed configuration block.

The following JSON configuration template optimizes DeepSpeed Stage 3 memory consumption:

<script src="https://gist.github.com/mohashari/48285f1412aa626dd4f2259b3c7ec0be.js?file=snippet-5.json"></script>

## Serving the Fine-Tuned Adapters at Scale

Once training is complete, the output artifacts consist of the trained LoRA adapter weights (which are typically less than 100MB) and configuration files. In a production environment, merging the adapter weights back into the 15GB base model is not recommended for two main reasons:

1. **Multi-Tenancy**: If you have 50 different tenants, each with their own specialized adapter, storing 50 copies of the base model requires 750GB of disk space and significant GPU memory.
2. **Deployment Latency**: Redeploying a new base model requires spinning up fresh pods with large image or weight downloads, leading to slow startup times.

Instead, we serve the adapters dynamically using a high-throughput engine like **vLLM**. vLLM keeps a single base model in memory and dynamically loads individual adapter weights on demand from cloud storage (e.g., Amazon S3).

The deployment configuration below sets up vLLM with multi-LoRA support:

<script src="https://gist.github.com/mohashari/48285f1412aa626dd4f2259b3c7ec0be.js?file=snippet-6.yaml"></script>

Once running, client requests can target specific adapter behavior by passing the adapter name in the standard OpenAI-compatible API request payload:

```json
{
  "model": "customer-support-adapter",
  "messages": [
    {"role": "user", "content": "How do I reset my API token?"}
  ]
}
```

This setup enables you to scale LLM training and serving workloads efficiently on Kubernetes. You can manage cost and compute usage by using KubeRay and Ludwig for training, and vLLM's dynamic multi-LoRA loading for low-latency inference.