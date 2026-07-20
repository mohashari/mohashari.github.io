---
layout: post
title: "Speculative Decoding in Custom LLM Servicing: Implementing Draft Models in Rust"
date: 2026-06-21 08:00:00 +0700
tags: [rust, llm-inference, performance, speculative-decoding]
description: "A production-focused guide to implementing speculative decoding with draft models in Rust, featuring tensor operations and KV cache rollback."
image: ""
thumbnail: ""
---

Autoregressive LLM generation in production is a memory bandwidth nightmare. When serving a model like Llama-3-70B in FP16, generating a single token requires reading 140 GB of weights from High Bandwidth Memory (HBM) to the GPU's SRAM. At a theoretical memory bandwidth limit of 2.0 TB/s on an NVIDIA A100 GPU, you are capped at roughly 14.5 tokens per second for a single request, leaving the tensor cores idling most of the time. While continuous batching helps amortize this cost across concurrent requests, single-user latency remains bound by memory bandwidth. Speculative decoding breaks this bottleneck. By using a lightweight draft model (e.g., Llama-3-8B) to speculatively generate a sequence of tokens, we can verify them in parallel in a single forward pass of the target model. This shifts the target model's execution from memory-bound to compute-bound, cutting latency by 1.8x to 2.5x in real-world API services.

## The Memory Bandwidth Wall and Speculative Math

To understand why speculative decoding works, we must analyze the arithmetic intensity of LLM generation. In the autoregressive generation phase, the model processes a single token at a time. The batch size is effectively small, meaning the ratio of floating-point operations to memory bytes transferred is extremely low. The GPU spends most of its time waiting for weights to load from HBM.

Speculative decoding bypasses this constraint by introducing a draft model $D$ (fast but less accurate) and a target model $T$ (slow but accurate). If we want to generate a sequence of tokens, we perform the following steps:
1. The draft model $D$ sequentially generates $K$ candidate tokens: $x_1, x_2, \dots, x_K$. This is fast because $D$ has far fewer parameters (e.g., 8B vs. 70B).
2. The target model $T$ runs a single forward pass on the prefix combined with all $K$ candidate tokens.
3. We stochastically verify the candidate tokens using the output distributions of both models.

Let $p(x)$ be the probability distribution of the target model, and $q(x)$ be the probability distribution of the draft model. For each candidate token $x_i$, we accept it with probability:

$$\alpha = \min\left(1.0, \frac{p(x_i)}{q(x_i)}\right)$$

If a token $x_i$ is accepted, we proceed to check $x_{i+1}$. If a token $x_i$ is rejected, we discard $x_i$ and all subsequent candidate tokens, and sample a new token from the adjusted distribution:

$$P'(x) = \frac{\max(0, p(x) - q(x))}{\sum_y \max(0, p(y) - q(y))}$$

This mathematical formulation guarantees that the final output distribution is identical to sampling directly from the target model. Thus, speculative decoding is a lossless optimization.

## Rust for Low-Latency LLM Servicing

While Python is the standard for machine learning prototyping, it introduces substantial overhead in production serving. Garbage collection pauses, Global Interpreter Lock (GIL) contention, and high memory footprints make it difficult to guarantee tight p99 latency SLAs. Custom Rust inference engines—leveraging libraries like `tch-rs` (Rust bindings for libtorch) or `candle`—allow developers to build memory-safe, ultra-low-latency serving systems. Rust gives us direct control over GPU allocations, memory layout, thread scheduling, and asynchronous task execution, which are critical when managing the concurrent execution of multiple models.

## KV Cache Rollback Mechanics

The primary engineering challenge in speculative decoding is managing the Key-Value (KV) cache. During standard autoregressive generation, keys and values are appended to the cache step-by-step. However, in speculative decoding, we append $K$ draft tokens to the cache, run validation, and then potentially reject some or all of them.

When a rejection occurs, we must roll back the KV cache pointer of both the draft and target models to match the number of accepted tokens. If we do not roll back the cache, the discarded tokens will pollute the attention context of future generation steps.

In a custom Rust engine using `tch-rs`, we pre-allocate contiguous KV cache tensors to avoid runtime allocation overhead. We track the logical length of the active sequence and use tensor slicing to overwrite rejected tokens in subsequent steps.

```rust
// snippet-1
// Managing the KV cache for speculative decoding. When draft tokens are rejected,
// we must rollback the cache pointer to the last accepted token state.
use std::sync::Arc;
use tch::{Tensor, Kind, Device};

pub struct KVCacheKeyVal {
    pub key: Tensor,
    pub value: Tensor,
    pub current_len: usize,
    pub max_len: usize,
}

impl KVCacheKeyVal {
    pub fn new(
        num_layers: usize,
        batch_size: usize,
        num_heads: usize,
        head_dim: usize,
        max_len: usize,
        device: Device,
    ) -> Self {
        let shape = [batch_size, num_heads, max_len, head_dim];
        let key = Tensor::zeros(&shape, (Kind::Float, device));
        let value = Tensor::zeros(&shape, (Kind::Float, device));
        Self {
            key,
            value,
            current_len: 0,
            max_len,
        }
    }

    /// Appends new KV projections to the cache. Returns the slice representing the updated state.
    pub fn append(&mut self, new_k: &Tensor, new_v: &Tensor) -> Result<(), String> {
        let append_len = new_k.size()[2] as usize;
        if self.current_len + append_len > self.max_len {
            return Err(format!(
                "KV Cache overflow: {} + {} > {}",
                self.current_len, append_len, self.max_len
            ));
        }

        let start = self.current_len as i64;
        let end = (self.current_len + append_len) as i64;
        
        // Tensor slicing: key[:, :, start:end, :] = new_k
        let mut k_slice = self.key.slice(2, start, end, 1);
        k_slice.copy_(new_k);

        let mut v_slice = self.value.slice(2, start, end, 1);
        v_slice.copy_(new_v);

        self.current_len += append_len;
        Ok(())
    }

    /// Rolls back the cache state to a specific sequence length.
    /// This is called when speculative tokens are rejected by the target model.
    pub fn rollback(&mut self, target_len: usize) -> Result<(), String> {
        if target_len > self.current_len {
            return Err(format!(
                "Cannot rollback forward: target_len {} > current_len {}",
                target_len, self.current_len
            ));
        }
        // Future writes will overwrite the rejected tokens' positions.
        self.current_len = target_len;
        Ok(())
    }
}
```

## The Verification Algorithm

The verification step takes the draft model's generated tokens, the draft model's probability distributions, and the target model's output logits. Since the target model evaluates all $K$ draft tokens in parallel, its output tensor includes the logits for $K+1$ positions (representing the prediction for the step after each draft token, plus the step following the final draft token).

We implement the stochastic verification algorithm using GPU operations. While it is tempting to copy these tensors back to the CPU to execute the loop, doing so introduces host-device synchronization barriers that destroy inference performance. Keep the distributions on the GPU, sample using multinomial operations, and only transfer the final accepted token count and correction token ID to the host.

```rust
// snippet-2
// Stochastic verification algorithm for speculative decoding.
// Compares target model probability distribution against draft model probability
// distribution to accept or reject drafted tokens according to the Leviathan et al. criteria.
use tch::Tensor;
use rand::Rng;

pub struct SpeculativeVerificationResult {
    pub accepted_count: usize,
    pub correction_token: i64,
}

/// Verifies a sequence of drafted tokens against the target model's logits.
/// - draft_tokens: [K] tensor containing the token IDs proposed by the draft model.
/// - draft_probs: [K, vocab_size] tensor of probability distributions from the draft model.
/// - target_logits: [K + 1, vocab_size] tensor of logits from the target model.
pub fn verify_draft_tokens(
    draft_tokens: &[i64],
    draft_probs: &Tensor,     // Shape: [K, Vocab]
    target_logits: &Tensor,   // Shape: [K + 1, Vocab]
    temperature: f64,
) -> SpeculativeVerificationResult {
    let k = draft_tokens.len();
    let mut accepted_count = 0;
    let mut rng = rand::thread_rng();

    // Convert target logits to probabilities: target_probs [K + 1, Vocab]
    let target_probs = (target_logits / temperature).softmax(-1, tch::Kind::Float);

    for i in 0..k {
        let drafted_token = draft_tokens[i];
        
        // Extract probability of the drafted token from target and draft distributions
        let target_prob_i = target_probs.double_value(&[i as i64, drafted_token]);
        let draft_prob_i = draft_probs.double_value(&[i as i64, drafted_token]);

        let ratio = target_prob_i / (draft_prob_i + 1e-9);
        let u: f64 = rng.gen();

        if u < f64::min(1.0, ratio) {
            accepted_count += 1;
        } else {
            // Rejected! Compute the normalized correction distribution:
            // P'(x) = max(0, P_target(x) - P_draft(x))
            let target_prob_dist = target_probs.select(0, i as i64);
            let draft_prob_dist = draft_probs.select(0, i as i64);
            
            let difference = (&target_prob_dist - &draft_prob_dist).clamp_min(0.0);
            let sum = difference.sum(tch::Kind::Float);
            
            let correction_token = if sum.double_value(&[]) > 1e-6 {
                let norm_dist = difference / sum;
                norm_dist.multinomial(1, true).int64_value(&[0])
            } else {
                target_prob_dist.multinomial(1, true).int64_value(&[0])
            };

            return SpeculativeVerificationResult {
                accepted_count,
                correction_token,
            };
        }
    }

    // All K tokens accepted. Sample the (K + 1)-th token from target_probs[K]
    let target_prob_last = target_probs.select(0, k as i64);
    let correction_token = target_prob_last.multinomial(1, true).int64_value(&[0]);

    SpeculativeVerificationResult {
        accepted_count,
        correction_token,
    }
}
```

## Specialized Causal Masking for Parallel Validation

When the target model validates the $K$ draft tokens, we submit them as a single sequence of length $K$ (or $K+1$ if we include the starting token). The self-attention mechanism must ensure that draft token $i$ can attend to the historical KV cache and draft tokens $0 \dots i-1$, but cannot attend to draft tokens $i+1 \dots K-1$.

To achieve this, we construct a custom speculative attention mask. Given historical KV cache length $L$ and draft sequence length $K$, the mask shape must be `[batch_size, 1, K, L + K]`.

For query position $q$ (where $0 \le q < K$) and key-value position $kv$ (where $0 \le kv < L + K$):
- If $kv < L$, it is a historical token; attention is allowed.
- If $kv \ge L$, it is a speculative draft token; attention is allowed only if $kv - L \le q$.

```rust
// snippet-3
// Constructs the specialized causal attention mask for parallel speculative verification.
// The target model must attend to the historical KV cache (length L) and the preceding
// drafted tokens, but must be masked from attending to subsequent drafted tokens.
use tch::{Device, Kind};

/// Generates the speculative attention mask.
/// - batch_size: number of active sequences in batch
/// - past_len: current length of the KV cache (L)
/// - spec_len: number of drafted tokens (K)
/// - device: target execution device (CPU/CUDA)
pub fn build_speculative_attention_mask(
    batch_size: i64,
    past_len: i64,
    spec_len: i64,
    device: Device,
) -> Tensor {
    let total_len = past_len + spec_len;
    
    // Range tensor for queries: [spec_len, 1]
    let q_indices = Tensor::arange(spec_len, (Kind::Int64, device)).unsqueeze(1);
    
    // Range tensor for speculative keys: [1, spec_len]
    let kv_spec_indices = Tensor::arange(spec_len, (Kind::Int64, device)).unsqueeze(0);
    
    // Speculative region mask: [spec_len, spec_len] -> 1 where q >= kv, else 0
    let spec_mask = q_indices.ge_tensor(&kv_spec_indices).to_kind(Kind::Float);
    
    // Past region mask: [spec_len, past_len] -> all 1s
    let past_mask = Tensor::ones(&[spec_len, past_len], (Kind::Float, device));
    
    // Concatenate along the key dimension to get [spec_len, past_len + spec_len]
    let mask_2d = Tensor::cat(&[past_mask, spec_mask], 1);
    
    // Expand to batch and attention heads dimensions: [batch_size, 1, spec_len, total_len]
    let mask_4d = mask_2d.unsqueeze(0).unsqueeze(1).expand(&[batch_size, 1, spec_len, total_len], true);
    
    // Convert binary mask (1.0 = attend, 0.0 = mask) to additive logit mask (0.0 = attend, -inf = mask)
    let inf_mask = (1.0 - mask_4d) * -1e9;
    
    inf_mask
}
```

## Orchestrating the Inference Loop

To implement speculative decoding without adding latency overhead, we must avoid a common design pitfall: running a separate single-step target forward pass to compute the KV cache entry for the correction token. 

Instead, we structure the target model's input in the next iteration to contain the correction token as its first element, followed by the new $K$ draft tokens. The batch size to the target model becomes $K+1$. The target model computes the KV projections for the correction token in parallel with the new verification pass.

Here is the implementation of the orchestration engine in Rust:

```rust
// snippet-4
// The orchestration loop coordinating the draft and target models.
// It feeds the correction token into the next validation batch, eliminating the need
// for an extra single-token target forward pass.
use std::sync::Arc;
use tch::Device;

pub trait LLMModel {
    fn forward(&self, tokens: &Tensor, mask: &Tensor, kv_cache: &mut KVCacheKeyVal) -> Result<Tensor, String>;
    fn device(&self) -> Device;
}

pub struct SpeculativeEngine {
    draft_model: Arc<dyn LLMModel + Send + Sync>,
    target_model: Arc<dyn LLMModel + Send + Sync>,
    temperature: f64,
    device: Device,
}

impl SpeculativeEngine {
    pub fn new(
        draft_model: Arc<dyn LLMModel + Send + Sync>,
        target_model: Arc<dyn LLMModel + Send + Sync>,
        temperature: f64,
    ) -> Self {
        let device = target_model.device();
        Self { draft_model, target_model, temperature, device }
    }

    pub fn generate_speculative(
        &self,
        prompt_tokens: &[i64],
        max_new_tokens: usize,
        k: usize,
    ) -> Result<Vec<i64>, String> {
        let mut generated = prompt_tokens.to_vec();
        
        let mut target_kv = KVCacheKeyVal::new(32, 1, 32, 128, 4096, self.device);
        let mut draft_kv = KVCacheKeyVal::new(12, 1, 16, 64, 4096, self.device);

        // Pre-fill phase is omitted for brevity; assume KV caches are populated with prompt.
        let mut last_accepted_token = *generated.last().ok_or("Empty prompt")?;
        let mut generated_count = 0;

        while generated_count < max_new_tokens {
            let mut draft_tokens = Vec::with_capacity(k);
            let mut draft_probs_list = Vec::with_capacity(k);
            
            // 1. Generate K draft tokens sequentially.
            let mut next_draft_input = Tensor::from_slice(&[last_accepted_token])
                .to_device(self.device)
                .unsqueeze(0);
            
            for _ in 0..k {
                let draft_mask = build_speculative_attention_mask(
                    1,
                    draft_kv.current_len as i64,
                    1,
                    self.device
                );
                let logits = self.draft_model.forward(&next_draft_input, &draft_mask, &mut draft_kv)?;
                let probs = (logits.select(1, -1) / self.temperature).softmax(-1, tch::Kind::Float);
                
                let sampled = probs.multinomial(1, true).int64_value(&[0]);
                draft_tokens.push(sampled);
                draft_probs_list.push(probs);
                
                next_draft_input = Tensor::from_slice(&[sampled]).to_device(self.device).unsqueeze(0);
            }
            let draft_probs = Tensor::cat(&draft_probs_list, 0);

            // 2. Parallel validation with the target model.
            // Feed the correction token + K draft tokens. Input length: K + 1.
            let mut target_input_vec = vec![last_accepted_token];
            target_input_vec.extend(&draft_tokens);
            
            let target_input = Tensor::from_slice(&target_input_vec).to_device(self.device).unsqueeze(0);
            let target_mask = build_speculative_attention_mask(
                1, 
                target_kv.current_len as i64, 
                (k + 1) as i64, 
                self.device
            );
            
            let target_logits = self.target_model.forward(&target_input, &target_mask, &mut target_kv)?;
            let target_logits_2d = target_logits.squeeze_dim(0); // Shape: [K + 1, Vocab]

            // 3. Verify draft tokens.
            let verification = verification_helper(
                &draft_tokens,
                &draft_probs,
                &target_logits_2d,
                self.temperature,
            );

            // 4. Rollback KV caches and prepare for next iteration.
            let accepted = verification.accepted_count;
            last_accepted_token = verification.correction_token;

            // Commit accepted draft tokens.
            for i in 0..accepted {
                generated.push(draft_tokens[i]);
            }
            generated.push(last_accepted_token);
            generated_count += accepted + 1;

            // Target KV cache rollback:
            // We appended K + 1 tokens. We keep the first (1 + accepted) tokens.
            let target_rollback_len = target_kv.current_len - (k + 1) + 1 + accepted;
            target_kv.rollback(target_rollback_len)?;

            // Draft KV cache rollback:
            // The draft model generated K tokens. We keep accepted tokens.
            let draft_rollback_len = draft_kv.current_len - k + accepted;
            draft_kv.rollback(draft_rollback_len)?;
        }

        Ok(generated)
    }
}

// Auxiliary helper to call verification
fn verification_helper(
    draft_tokens: &[i64],
    draft_probs: &Tensor,
    target_logits: &Tensor,
    temp: f64,
) -> SpeculativeVerificationResult {
    verify_draft_tokens(draft_tokens, draft_probs, target_logits, temp)
}
```

## Production Pitfalls & Mitigation Strategies

While speculative decoding offers huge latency gains on paper, deploying it in high-throughput production environments exposes several critical bottlenecks:

### 1. CPU-GPU Synchronization Barriers

If your speculative engine checks the acceptance criteria on the CPU, you will force host-device synchronizations (e.g., calling `.double_value()` on GPU tensors inside the loop). In Rust, each sync blocks the execution thread and stalls the GPU pipeline.

**Mitigation:** Write the acceptance checks using PyTorch/LibTorch operators that run entirely on the GPU. Only return a tiny 2-element tensor to the CPU containing the `accepted_count` and `correction_token`.

### 2. KV Cache Memory Fragmentation

Under continuous batching, pre-allocating contiguous tensor segments per request leads to severe memory fragmentation. If a request rolls back its KV cache, the contiguous block is partially unused, but cannot be easily shared.

**Mitigation:** Implement a block-based memory allocator (PagedAttention). The KV cache is divided into fixed-size physical blocks (e.g., block size = 16 tokens). When rolling back, the block manager simply frees the physical blocks that map to indices beyond the rollback length.

### 3. Acceptance Rate Degradation

Speculative decoding relies on high acceptance rates. If the draft model accepts fewer than 1.5 tokens per step on average, the overhead of draft generation and parallel validation exceeds the latency of standard autoregressive generation. Acceptance rates drop dramatically during highly technical prompts, code generation, or when the temperature is set high.

**Mitigation:** Implement an adaptive draft lookahead $K$. Monitor the moving average acceptance rate per request. If it falls below a threshold (e.g., 40%), dynamically reduce $K$ from 5 to 2. If it increases, scale it back up.

To monitor these conditions, you must instrument your inference loop. The following Rust snippet shows how to integrate Prometheus metrics to track acceptance rates and phase latencies:

```rust
// snippet-5
// Prometheus instrumentation for tracking speculative decoding efficiency in production.
// It tracks acceptance rate, latency of draft vs. target passes, and token generation speed.
use prometheus::{HistogramOpts, HistogramVec, IntCounter, Registry};
use std::time::Duration;

pub struct SpeculativeMetrics {
    pub tokens_generated: IntCounter,
    pub tokens_drafted: IntCounter,
    pub tokens_accepted: IntCounter,
    pub draft_latency: HistogramVec,
    pub target_latency: HistogramVec,
}

impl SpeculativeMetrics {
    pub fn register(registry: &Registry) -> Result<Self, prometheus::Error> {
        let tokens_generated = IntCounter::new(
            "speculative_tokens_generated_total",
            "Total tokens emitted to client"
        )?;
        let tokens_drafted = IntCounter::new(
            "speculative_tokens_drafted_total",
            "Total draft tokens proposed by draft model"
        )?;
        let tokens_accepted = IntCounter::new(
            "speculative_tokens_accepted_total",
            "Total draft tokens accepted by target model"
        )?;
        
        let draft_latency = HistogramVec::new(
            HistogramOpts::new(
                "speculative_draft_latency_seconds",
                "Latency of the draft model execution phase"
            )
            .buckets(vec![0.001, 0.002, 0.005, 0.010, 0.020, 0.050]),
            &["step"]
        )?;
        
        let target_latency = HistogramVec::new(
            HistogramOpts::new(
                "speculative_target_latency_seconds",
                "Latency of the target validation phase"
            )
            .buckets(vec![0.005, 0.010, 0.020, 0.050, 0.100, 0.200]),
            &["batch_size"]
        )?;

        registry.register(Box::new(tokens_generated.clone()))?;
        registry.register(Box::new(tokens_accepted.clone()))?;
        registry.register(Box::new(draft_latency.clone()))?;
        registry.register(Box::new(target_latency.clone()))?;

        Ok(Self {
            tokens_generated,
            tokens_drafted,
            tokens_accepted,
            draft_latency,
            target_latency,
        })
    }

    /// Record metrics for a single generation step.
    pub fn record_step(
        &self,
        drafted: usize,
        accepted: usize,
        draft_dur: Duration,
        target_dur: Duration,
    ) {
        self.tokens_generated.inc_by((accepted + 1) as u64);
        self.tokens_drafted.inc_by(drafted as u64);
        self.tokens_accepted.inc_by(accepted as u64);
        
        self.draft_latency
            .with_label_values(&["sequential_draft"])
            .observe(draft_dur.as_secs_f64());
            
        self.target_latency
            .with_label_values(&[&drafted.to_string()])
            .observe(target_dur.as_secs_f64());
    }
}
```
