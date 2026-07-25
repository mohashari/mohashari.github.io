---
layout: post
title: "Designing a Zero-Allocation Circular Buffer Log Writer in C++ Using Atomic Memory Operations and Memory-Mapped Files"
category: software_engineering
date: 2026-07-25 08:00:00 +0700
tags: [cpp, systems-programming, performance, low-latency]
description: "Build a lock-free, zero-allocation circular buffer log writer in C++ using atomic memory operations and memory-mapped files to sustain millions of logs/sec."
image: "https://picsum.photos/seed/639/1080/720"
thumbnail: "https://picsum.photos/seed/639/400/300"
---

In low-latency systems such as electronic trading matching engines or real-time packet processing pipelines, logging is often the silent killer of tail latency. Under a load of 1,000,000 events per second, conventional logging frameworks fail catastrophically. A single `std::string` heap allocation on the critical path can introduce latency spikes from 50 nanoseconds to over 10 microseconds. Furthermore, invoking a standard `write` system call switches the CPU context into kernel space, potentially blocking the thread if the page cache is locked or dirty pages are being written back to physical disk. To preserve sub-microsecond execution budgets, we must avoid heap allocations, mutex contention, and system calls. This post details how to design a zero-allocation, multi-producer, single-consumer (MPSC) circular log writer that writes directly to disk via memory-mapped files (`mmap`) using C++ atomic operations.

## The Architecture: Memory-Mapped Ring Buffers

To bypass system calls entirely, we use memory-mapped files (`mmap`). This maps a physical file directly into the virtual memory address space of the application. Writing to this region updates the kernel’s page cache directly in RAM. If the application crashes, the OS kernel retains the dirty pages and flushes them to disk asynchronously, providing crash resilience without the synchronous latency cost of write calls.

To prevent write contention among threads, we structure this mapped memory as a circular ring buffer. This buffer contains a single control block containing metadata indices followed by an array of fixed-size slots. A single background consumer thread sweeps through these slots, processes or formats the payloads, and advances the read index.

Because multiple threads write concurrently while a background thread reads, we must prevent false sharing. False sharing occurs when threads on different CPU cores modify variables that reside within the same 64-byte cache line. This invalidates the cache line across cores, causing severe memory bus stalls. We prevent false sharing by aligning our indices and message slots to 64-byte cache lines using `alignas(64)`.

Here is the binary layout for our mapped circular buffer:

<script src="https://gist.github.com/mohashari/d6c4ac0a387708eba99a31aea72d9459.js?file=snippet-1.txt"></script>

## Establishing the Memory Mapping Layer

When mapping files for low-latency operations, naive usage of `mmap` can lead to major latency spikes. A default file expansion using `ftruncate` creates a sparse file. When a thread writes to a sparse page for the first time, it triggers a "hard page fault." The kernel halts the user-space thread, allocates a physical page on disk, updates the page table, and only then resumes execution. This page fault can block your thread for several milliseconds.

To prevent this in production:
1. Use `posix_fallocate` to allocate physical disk blocks immediately upon file creation.
2. Use `MAP_POPULATE` during the `mmap` call to pre-fault the pages and populate the process page tables at startup.
3. Advise the kernel with `madvise` and `MADV_SEQUENTIAL | MADV_WILLNEED` to signal sequential access patterns.

Below is the RAII implementation for initializing this memory-mapped log file:

<script src="https://gist.github.com/mohashari/d6c4ac0a387708eba99a31aea72d9459.js?file=snippet-2.txt"></script>

## Atomic Slot Reservation

In a multi-producer scenario, threads compete to reserve a slot in the ring buffer. We must avoid mutexes to eliminate locking overhead and thread scheduling pauses. Instead, threads reserve a slot index using a lock-free compare-and-swap (`compare_exchange_weak`) loop on the `write_index` metadata.

If the buffer is full—meaning the gap between the `write_index` and the `read_index` has reached buffer capacity—the reservation fails immediately. The calling application can then decide whether to block, log to a fallback memory buffer, or discard the message.

Here is the implementation of the reservation protocol:

<script src="https://gist.github.com/mohashari/d6c4ac0a387708eba99a31aea72d9459.js?file=snippet-3.txt"></script>

### Understanding the Memory Orderings
- **`std::memory_order_acquire` on `read_index`**: Ensures that the writer threads see all changes made by the consumer thread (e.g., clearing the slot status back to empty) prior to reusing that slot.
- **`std::memory_order_release` on `write_index`**: Guarantees that all previous reads and writes in the current thread are visible to other threads before the incremented index becomes globally visible.

## Zero-Allocation Writing & Spinlocks

Once a thread has reserved a slot index, it must copy the data into the slot. However, because slot reservations are atomic, thread writes can execute out-of-order. A thread reserving slot 5 might complete its write *after* another thread reserving slot 6 finishes its write. 

To prevent the consumer thread from reading a partially written slot, we implement a state transition protocol using the slot's atomic `status` variable:
1. **Status 0 (Empty)**: The slot is available for writers.
2. **Status 1 (Writing)**: A writer thread has claimed the slot and is copying data.
3. **Status 2 (Ready)**: The write is complete, and the slot is ready for consumption.

If a slot is wrapped around but the consumer hasn't finished reading it, the writer spins until the status is reset to `0`. We emit CPU pause instructions (`__builtin_ia32_pause()` on x86 or `yield` on ARM) inside spin loops to prevent pipeline stalls and reduce CPU power consumption.

<script src="https://gist.github.com/mohashari/d6c4ac0a387708eba99a31aea72d9459.js?file=snippet-4.txt"></script>

## The Asynchronous Consumer Thread

The background consumer thread runs in a loop, sequentializing log reads. It polls the slot at the current `read_index`. Once the slot transitions to state `2` (Ready), the consumer accesses the payload directly in the mapped region, writes it to secondary storage (such as a compressed file or a network socket), resets the slot status to `0` (Empty), and advances the `read_index`.

<script src="https://gist.github.com/mohashari/d6c4ac0a387708eba99a31aea72d9459.js?file=snippet-5.txt"></script>

## Crash Recovery and Validation

A primary advantage of storing our circular buffer directly in a mapped file is crash recovery. When an application process crashes due to a segmentation fault, the operating system keeps the memory mapping alive in RAM and flushes it to disk. 

When the application restarts, it reads the metadata header. To restore consistency, we must identify and repair "torn writes"—slots left in state `1` (Writing) when the process crashed. We search for these slots and reset their status to `0` while rolling back the global `write_index` to prevent data corruption.

<script src="https://gist.github.com/mohashari/d6c4ac0a387708eba99a31aea72d9459.js?file=snippet-6.txt"></script>

## Zero-Allocation Logging Formatting

To achieve true zero-allocation execution, we must avoid C++ formatting options like `std::stringstream` or standard formatting mechanisms that allocate dynamic strings. Instead, use `std::to_chars` or `{fmt}` to format variables directly into the pre-allocated log slot payload.

Below is a utility to format a fast, zero-allocation log entry directly into a raw character buffer:

<script src="https://gist.github.com/mohashari/d6c4ac0a387708eba99a31aea72d9459.js?file=snippet-7.txt"></script>

## Linux Kernel Pitfalls & System Tuning

Even optimized lock-free C++ code can suffer from latency spikes if the underlying Linux kernel is not tuned for low-latency operations. Below are the three most common OS pitfalls when using memory-mapped files.

### 1. Page Cache Writeback Stalls
By default, the Linux kernel periodically flushes dirty pages in the background. When the number of dirty pages in memory exceeds `/proc/sys/vm/dirty_background_ratio` (default is 10%), background kernel threads start flushing pages to disk. If the rate of logging is high and dirty pages exceed `/proc/sys/vm/dirty_ratio` (default is 20%), the kernel blocks user threads to prioritize disk flushes. This stalls log writers.

To prevent this, isolate your logging files to a dedicated high-end NVMe drive mounted with `noatime,nodiratime` and adjust dirty page ratios in your sysctl settings to enforce continuous, low-volume background flushing:
```bash
# Force frequent, low-volume writebacks to eliminate massive flush pauses
sysctl -w vm.dirty_background_ratio=3
sysctl -w vm.dirty_ratio=10
```

### 2. TLB Cache Misses
A standard memory page size in Linux is 4KB. If your log buffer is 1GB, the virtual-to-physical address mappings require 262,144 page entries. Accessing these pages across thread context changes triggers translation lookaside buffer (TLB) misses, causing CPU stalls.

To mitigate this, configure your memory-mapped log file to use Huge Pages (typically 2MB). In your C++ allocation, pass the `MAP_HUGETLB` flag:

<script src="https://gist.github.com/mohashari/d6c4ac0a387708eba99a31aea72d9459.js?file=snippet-8.txt"></script>

### 3. SIGBUS Signals
A major failure mode with `mmap` is the `SIGBUS` (Bus Error) signal. This occurs if your application attempts to access a virtual address in the mapped range that exceeds the actual size of the physical file. This can happen if another process truncates the file, or if the drive runs out of space (`ENOSPC`) during a writeback.

To protect your system from crashing under `SIGBUS`:
- Always pre-allocate file space with `posix_fallocate` before calling `mmap`.
- Register a signal handler for `SIGBUS` that dumps debugging diagnostic info and exits cleanly instead of crashing abruptly.

## Conclusion

By using atomic operations to claim memory slots, memory-mapping files to bypass the kernel write path, and employing zero-allocation formatting, you can build a highly optimized C++ logging utility. This architecture removes system call overhead and lock contention from the critical path, keeping your p99.99 latencies predictable even under extreme load.