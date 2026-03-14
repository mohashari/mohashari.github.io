---
layout: post
title: "eBPF for Backend Engineers: Deep Observability Without Instrumentation"
date: 2026-04-04 07:00:00 +0700
tags: [ebpf, observability, linux, performance, networking]
description: "Leverage eBPF to trace system calls, profile applications, and debug network issues at the kernel level with zero application changes."
---

Every backend engineer has faced the same nightmare: production is slow, your dashboards show nothing obviously wrong, and adding more logging means redeploying services that are already struggling. Traditional observability requires you to instrument your code in advance, predict what you'll need to know, and redeploy when you're wrong. eBPF (Extended Berkeley Packet Filter) breaks this constraint entirely. It lets you attach small, sandboxed programs directly to the Linux kernel, tracing system calls, network events, and function calls in running processes — no recompilation, no restarts, no application changes whatsoever. For backend engineers debugging production systems, this is transformative.

## What eBPF Actually Is

eBPF started as a packet filtering mechanism (the "BPF" in `tcpdump`) but has evolved into a general-purpose in-kernel virtual machine. You write a program in a restricted subset of C, compile it to eBPF bytecode, and the kernel verifier checks it for safety before loading it. The program attaches to a hook — a syscall, a kernel function, a network interface, a user-space probe — and executes whenever that hook fires. Data flows back to user space through ring buffers and maps.

The kernel verifier is the key safety guarantee: it statically analyzes your program to ensure no infinite loops, no invalid memory access, and bounded execution time. This is why eBPF can run in production without risk of crashing the kernel, unlike traditional kernel modules.

The primary tools you'll use as a backend engineer are `bpftrace` for quick one-liners and exploration, `libbpf` with C or Go for production tooling, and `BCC` (BPF Compiler Collection) for Python-based scripts. Let's work through practical scenarios.

## Tracing Slow System Calls

The first thing you want when debugging latency is knowing which syscalls are slow. This `bpftrace` one-liner traces all `read()` calls taking longer than 1ms in a given process:

<script src="https://gist.github.com/mohashari/5bc22f9fe9a3586a9253273756226b39.js?file=snippet.sh"></script>

This runs entirely in the kernel, sampling only the slow outliers. You'll immediately see whether your latency is in the syscall itself or elsewhere, and the histogram gives you percentile distribution without any application changes.

## Profiling CPU Hotspots with Flame Graphs

Off-CPU profiling — finding where processes are *waiting* rather than running — is notoriously hard without eBPF. This script captures off-CPU time stacks and feeds them into Brendan Gregg's flame graph tooling:

<script src="https://gist.github.com/mohashari/5bc22f9fe9a3586a9253273756226b39.js?file=snippet-2.sh"></script>

The resulting SVG shows you exactly where your processes spend time blocked on I/O, locks, or sleeps — the invisible latency that CPU profilers miss entirely.

## Observing Network Connections with libbpf in Go

For production tooling, you want a proper program rather than a one-liner. This Go program using `cilium/ebpf` (the idiomatic Go eBPF library) tracks TCP connection establishments to help debug connection pool exhaustion:

<script src="https://gist.github.com/mohashari/5bc22f9fe9a3586a9253273756226b39.js?file=snippet-3.go"></script>

This gives you a real-time stream of every outbound TCP connection your application makes, including which goroutine (via PID) initiated it. Connection pool bugs that manifest as thousands of short-lived connections become immediately visible.

## Tracing Database Query Latency via USDT Probes

Many runtimes expose USDT (Userland Statically Defined Tracing) probes. PostgreSQL is a great example — you can trace query execution without touching pg_stat_statements:

<script src="https://gist.github.com/mohashari/5bc22f9fe9a3586a9253273756226b39.js?file=snippet-4.sh"></script>

You're reading directly from the PostgreSQL binary's probe points. No query logging overhead, no log parsing, just the slow queries you care about surfaced in real time.

## Detecting File Descriptor Leaks

FD leaks are subtle and accumulate slowly. This script tracks open/close pairs and reports processes with growing imbalances:

<script src="https://gist.github.com/mohashari/5bc22f9fe9a3586a9253273756226b39.js?file=snippet-5.sh"></script>

## Packaging eBPF Tools in a Sidecar

For Kubernetes environments, you can run eBPF tooling as a privileged sidecar or DaemonSet without touching your application pods:

<script src="https://gist.github.com/mohashari/5bc22f9fe9a3586a9253273756226b39.js?file=snippet-6.yaml"></script>

This DaemonSet pattern means you deploy your eBPF observer once per node and it can observe every pod without any application changes — the exact zero-instrumentation promise of eBPF fulfilled at the infrastructure level.

## Building a Minimal Observer Container

To containerize your `bpftrace` scripts for the DaemonSet above:

<script src="https://gist.github.com/mohashari/5bc22f9fe9a3586a9253273756226b39.js?file=snippet-7.dockerfile"></script>

eBPF represents a fundamental shift in how we think about observability. Instead of planning instrumentation upfront and hoping you captured the right signals, you now have the ability to ask any question about your running system at any time, with kernel-level precision and negligible overhead. The practical starting point is `bpftrace` one-liners — they require no build step and answer questions in seconds. As your needs mature, `cilium/ebpf` in Go gives you production-grade tooling that compiles once and runs on any kernel supporting BTF (5.4+, which covers every major distribution since 2020). The real payoff comes when you stop thinking of observability as something you bake into your application and start thinking of it as an infrastructure concern that can be applied anywhere, anytime, without asking anyone to redeploy.