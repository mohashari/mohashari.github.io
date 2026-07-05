---
layout: post
title: "Implementing a Lock-Free Circular Queue for High-Frequency Trading Systems in C++"
date: 2026-07-05 08:00:00 +0700
tags: [cpp, concurrency, low-latency, hft]
description: "A deep dive into engineering high-throughput, low-latency lock-free circular queues in C++ for production high-frequency trading systems."
image: "https://picsum.photos/seed/3177/1080/720"
thumbnail: "https://picsum.photos/seed/3177/400/300"
---

In high-frequency trading (HFT) systems, a 10-microsecond delay in processing an order book update can mean the difference between capturing a multi-million dollar arbitrage opportunity or executing a toxic trade. Standard synchronization primitives like `std::mutex` rely on the kernel's scheduler. If a thread encounters contention, the operating system performs a context switch. This context switch wastes up to 10 microseconds, flushes translation lookaside buffers (TLBs), and evicts hot data from L1/L2 caches. For pipelines handling millions of packets per second, lock-free data structures—specifically Single-Producer Single-Consumer (SPSC) and Multi-Producer Multi-Consumer (MPMC) circular queues—are not a micro-optimization; they are a fundamental architectural requirement. This post dissects the implementation details of production-grade, cache-aligned, lock-free circular queues in C++20, examining memory barriers, compiler reordering, cache coherency protocols, and profiling strategies.

## The Cost of Traditional Synchronization

To understand why lock-free structures are mandatory, we must quantify the latency overhead of kernel-level synchronization. When a thread fails to acquire a mutex, the Linux kernel puts the thread to sleep using a `futex` system call. The cost of a context switch involves:

1. **User-to-Kernel Space Transition:** Crossing the privilege boundary.
2. **Scheduler Overhead:** The kernel must select a new thread to run.
3. **Register and State Saving:** Saving the CPU state (AVX-512 registers, instruction pointers).
4. **Cache Eviction:** The newly scheduled thread evicts the hot cache lines of the trading loop.

An uncontended mutex lock/unlock cycle takes roughly 15 to 20 nanoseconds. However, under high contention, the cost spikes to 2,000 to 10,000 nanoseconds. By contrast, a lock-free queue operating in user-space uses atomic instructions (like `std::atomic::compare_exchange_weak`) and memory ordering to coordinate access without blocking. This keeps operations in the sub-10 nanosecond range, even under heavy load.

## Cache Coherency and the False Sharing Trap

Modern CPUs retrieve data from main memory in blocks called cache lines (typically 64 bytes on x86-64 and ARM). When a CPU core modifies a variable, the cache coherency protocol (MESI or MOESI) invalidates that entire cache line in the caches of all other cores. 

If the write pointer (`write_idx_`) and the read pointer (`read_idx_`) of a circular queue reside on the same cache line, a phenomenon known as **false sharing** occurs. Even though the producer thread only modifies `write_idx_` and the consumer thread only modifies `read_idx_`, the CPU cores executing these threads will constantly bounce the cache line back and forth. This cache-line bouncing degrades throughput and increases latency.

To prevent false sharing, we must align and pad these variables to distinct cache lines using `alignas`.

// snippet-1
#include <atomic>
#include <new>

#if defined(__cpp_lib_hardware_interference_size)
    using std::hardware_destructive_interference_size;
#else
    // Fallback to 64 bytes, typical for modern x86/ARM CPUs
    constexpr size_t hardware_destructive_interference_size = 64;
#endif

template <typename T, size_t Capacity>
struct CacheAlignedQueueLayout {
    static_assert((Capacity & (Capacity - 1)) == 0, "Capacity must be a power of two");

    // Align storage to prevent false sharing with control structures
    alignas(hardware_destructive_interference_size) T storage_[Capacity];

    // Read index aligned to its own cache line
    alignas(hardware_destructive_interference_size) std::atomic<size_t> read_idx_{0};

    // Write index aligned to its own cache line to prevent cache line bouncing
    alignas(hardware_destructive_interference_size) std::atomic<size_t> write_idx_{0};
};

## Memory Barriers and the CPU Memory Model

CPUs and compilers aggressively reorder operations to optimize execution speed. 
* **Compilers** reorder instructions during optimization phases (e.g., instruction scheduling, loop-invariant code motion) if they determine that the reordering does not affect single-threaded correctness.
* **CPUs** execute instructions out-of-order via Reorder Buffers (ROB) to hide memory access latencies.

On x86-64, the hardware implements a Total Store Order (TSO) memory model. Stores are not reordered with other stores, and loads are not reordered with other loads. However, on weakly ordered architectures like ARM (e.g., AWS Graviton or Apple M-series), the CPU can reorder almost any memory operation.

In C++, we control these behaviors using memory ordering constraints. The default memory order is `std::memory_order_seq_cst` (sequentially consistent), which establishes a strict global ordering. While safe, it generates expensive memory fences (`MFENCE` or lock-prefixed instructions on x86, `DMB` on ARM) that drain the CPU's store buffers.

For maximum performance, we must use acquire-release semantics:
* **Release Store (`std::memory_order_release`):** Ensures that all preceding writes (e.g., writing the payload to the queue slot) are committed to cache/memory before the index is updated.
* **Acquire Load (`std::memory_order_acquire`):** Ensures that subsequent reads (e.g., reading the payload from the queue slot) cannot occur until the index update is observed.

## Implementing a Production-Grade SPSC Queue

A Single-Producer Single-Consumer (SPSC) queue is the most efficient queue type because it eliminates the need for expensive Compare-And-Swap (CAS) loops. Since only one thread writes to `write_idx_` and only one thread writes to `read_idx_`, we can implement synchronization using relaxed loads, acquire loads, and release stores.

To optimize the implementation further, we can cache the peer pointer locally. The producer thread reads the consumer's `read_idx_` only when the queue appears to be full. This avoids reading the atomic variable across cache lines on every push.

// snippet-2
#include <atomic>
#include <new>
#include <utility>
#include <cstddef>

template <typename T, size_t Capacity>
class LockFreeSPSCQueue {
    static_assert((Capacity & (Capacity - 1)) == 0, "Capacity must be a power of two");
    static constexpr size_t Mask = Capacity - 1;
    static constexpr size_t CacheLine = 64;

    // Use uninitialized storage to avoid default-constructing elements
    struct Node {
        alignas(alignof(T)) char storage[sizeof(T)];
    };

    alignas(CacheLine) Node ring_[Capacity];

    // Producer alignment area
    alignas(CacheLine) std::atomic<size_t> write_idx_{0};
    alignas(CacheLine) size_t cached_read_idx_{0};

    // Consumer alignment area
    alignas(CacheLine) std::atomic<size_t> read_idx_{0};
    alignas(CacheLine) size_t cached_write_idx_{0};

public:
    LockFreeSPSCQueue() = default;

    ~LockFreeSPSCQueue() {
        T dummy;
        while (pop(dummy));
    }

    // Delete copy/move constructors for safety
    LockFreeSPSCQueue(const LockFreeSPSCQueue&) = delete;
    LockFreeSPSCQueue& operator=(const LockFreeSPSCQueue&) = delete;

    template <typename... Args>
    bool emplace(Args&&... args) {
        const size_t current_write = write_idx_.load(std::memory_order_relaxed);
        const size_t limit = current_write - Capacity;

        // If cached read index says we are full, pull the latest atomic value
        if (cached_read_idx_ <= limit) {
            cached_read_idx_ = read_idx_.load(std::memory_order_acquire);
            if (cached_read_idx_ <= limit) {
                return false; // Queue is full
            }
        }

        new (&ring_[current_write & Mask].storage) T(std::forward<Args>(args)...);
        write_idx_.store(current_write + 1, std::memory_order_release);
        return true;
    }

    bool push(const T& item) {
        return emplace(item);
    }

    bool push(T&& item) {
        return emplace(std::move(item));
    }

    bool pop(T& value) {
        const size_t current_read = read_idx_.load(std::memory_order_relaxed);

        // If cached write index says we are empty, pull the latest atomic value
        if (cached_write_idx_ <= current_read) {
            cached_write_idx_ = write_idx_.load(std::memory_order_acquire);
            if (cached_write_idx_ <= current_read) {
                return false; // Queue is empty
            }
        }

        T* item_ptr = reinterpret_cast<T*>(&ring_[current_read & Mask].storage);
        value = std::move(*item_ptr);
        item_ptr->~T();

        read_idx_.store(current_read + 1, std::memory_order_release);
        return true;
    }
};

## Scaling to Multi-Producer Multi-Consumer (MPMC)

In systems where multiple threads write order updates or consume execution reports, an SPSC queue is insufficient. An MPMC queue requires synchronizing multiple active writers and readers. 

A common way to construct an array-based MPMC queue is Dmitry Vyukov's bounded queue. It assigns a sequence number to each slot in the ring buffer. Producers and consumers atomically advance the global indices and spin-wait until the slot's sequence matches their expected step.

// snippet-3
#include <atomic>
#include <new>
#include <utility>
#include <vector>

template <typename T>
class LockFreeMPMCQueue {
    struct Cell {
        std::atomic<size_t> sequence;
        alignas(alignof(T)) char data[sizeof(T)];
    };

    static constexpr size_t CacheLineSize = 64;

    alignas(CacheLineSize) std::vector<Cell> buffer_;
    const size_t capacity_;
    const size_t mask_;

    alignas(CacheLineSize) std::atomic<size_t> enqueue_idx_{0};
    alignas(CacheLineSize) std::atomic<size_t> dequeue_idx_{0};

public:
    explicit LockFreeMPMCQueue(size_t capacity)
        : capacity_(capacity), mask_(capacity - 1), buffer_(capacity) {
        // Capacity must be a power of two
        for (size_t i = 0; i < capacity_; ++i) {
            buffer_[i].sequence.store(i, std::memory_order_relaxed);
        }
    }

    ~LockFreeMPMCQueue() {
        T val;
        while (dequeue(val));
    }

    bool enqueue(const T& val) {
        Cell* cell;
        size_t pos = enqueue_idx_.load(std::memory_order_relaxed);
        for (;;) {
            cell = &buffer_[pos & mask_];
            size_t seq = cell->sequence.load(std::memory_order_acquire);
            intptr_t diff = static_cast<intptr_t>(seq) - static_cast<intptr_t>(pos);
            if (diff == 0) {
                if (enqueue_idx_.compare_exchange_weak(pos, pos + 1, std::memory_order_relaxed)) {
                    break;
                }
            } else if (diff < 0) {
                return false; // Queue is full
            } else {
                pos = enqueue_idx_.load(std::memory_order_relaxed);
            }
        }

        new (&cell->data) T(val);
        cell->sequence.store(pos + 1, std::memory_order_release);
        return true;
    }

    bool dequeue(T& val) {
        Cell* cell;
        size_t pos = dequeue_idx_.load(std::memory_order_relaxed);
        for (;;) {
            cell = &buffer_[pos & mask_];
            size_t seq = cell->sequence.load(std::memory_order_acquire);
            intptr_t diff = static_cast<intptr_t>(seq) - static_cast<intptr_t>(pos + 1);
            if (diff == 0) {
                if (dequeue_idx_.compare_exchange_weak(pos, pos + 1, std::memory_order_relaxed)) {
                    break;
                }
            } else if (diff < 0) {
                return false; // Queue is empty
            } else {
                pos = dequeue_idx_.load(std::memory_order_relaxed);
            }
        }

        T* item_ptr = reinterpret_cast<T*>(&cell->data);
        val = std::move(*item_ptr);
        item_ptr->~T();
        cell->sequence.store(pos + mask_ + 1, std::memory_order_release);
        return true;
    }
};

## CPU Core Pinning and Thread Affinity

Even with a perfectly written lock-free queue, your code will suffer latency spikes (jitter) if the operating system's Completely Fair Scheduler (CFS) migrates your thread to a different CPU core. Thread migration invalidates L1 and L2 caches, causing latency spikes in the range of 5 to 50 microseconds.

To guarantee deterministic sub-microsecond runtimes, you must:
1. Isolate target CPU cores at the boot level (using the kernel parameter `isolcpus`).
2. Pin the producer and consumer threads to specific isolated physical cores.

// snippet-4
#include <pthread.h>
#include <sched.h>
#include <thread>
#include <system_error>

bool pin_thread_to_core(std::thread& th, int core_id) {
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(core_id, &cpuset);
    
    int rc = pthread_setaffinity_np(th.native_handle(), sizeof(cpu_set_t), &cpuset);
    if (rc != 0) {
        return false;
    }
    return true;
}

## Benchmarking SPSC vs MPMC Performance

To measure queue performance, we use Google Benchmark to establish throughput and latency metrics. The following benchmark compares the latency of the lock-free SPSC queue against our MPMC implementation.

// snippet-5
#include <benchmark/benchmark.h>
#include <thread>
#include <vector>

// Assumes LockFreeSPSCQueue is defined
static void BM_SPSC_Latency(benchmark::State& state) {
    static LockFreeSPSCQueue<int, 65536> queue;
    const int num_iterations = state.range(0);

    if (state.thread_index() == 0) {
        // Producer Thread
        for (auto _ : state) {
            for (int i = 0; i < num_iterations; ++i) {
                while (!queue.push(i)) {
                    #if defined(__x86_64__)
                        asm volatile("pause" ::: "memory");
                    #elif defined(__aarch64__)
                        asm volatile("yield" ::: "memory");
                    #endif
                }
            }
        }
    } else {
        // Consumer Thread
        for (auto _ : state) {
            int value = 0;
            for (int i = 0; i < num_iterations; ++i) {
                while (!queue.pop(value)) {
                    #if defined(__x86_64__)
                        asm volatile("pause" ::: "memory");
                    #elif defined(__aarch64__)
                        asm volatile("yield" ::: "memory");
                    #endif
                }
            }
        }
    }
}

BENCHMARK(BM_SPSC_Latency)->Arg(1000000)->Threads(2);
BENCHMARK_MAIN();

## Profiling and Identifying Contention in Production

False sharing can slip past compiler warnings and code reviews. To identify cache line contention in production binaries, use the Linux `perf` suite. `perf c2c` (Cache-to-Cache) records memory access details and flags contested cache lines.

// snippet-6
# // Record memory access events with load/store tracking
# perf record -e cpu/mem-loads,ldlat=30/pp -e cpu/mem-stores/pp -a -g -- ./trading_engine_binary
# 
# // Analyze the recorded traces to identify false sharing hot-spots
# perf c2c report --stdio

When analyzing the output of `perf c2c`, inspect the "HitM" (Hit Modified) column. A high HitM count indicates that a core requested data that was modified in another core's cache, forcing a slow cache line transfer over the interconnect bus (e.g., Intel UPI or AMD Infinity Fabric).

Additionally, execute testing builds with ThreadSanitizer (TSan) enabled to catch memory order violations and data races before deploying to production.

// snippet-7
# # Compile with ThreadSanitizer instrumentation
# clang++ -O2 -g -fsanitize=thread -std=c++20 main.cpp -o tsan_test
# 
# # Run the binary to catch race conditions and memory ordering bugs
# TSAN_OPTIONS="second_deadlock_detection=1" ./tsan_test

## Real-World Failure Modes & Pitfalls

### Memory Reordering on Non-x86 Hardware

A common trap for C++ developers working on x86-64 servers is using `std::memory_order_relaxed` where `std::memory_order_acquire` or `std::memory_order_release` is required. Because x86-64 enforces Total Store Order at the hardware level, these relaxed operations do not get reordered by the CPU. The queue may run perfectly in local testing.

However, if you deploy this same binary to an ARM-based cloud instance (such as AWS Graviton), the weak memory model allows the CPU to reorder reads and writes. The consumer might read stale data before the producer finishes writing it, resulting in corrupt packets or invalid order IDs. Always validate lock-free algorithms on ARM platforms or run them under hardware emulators.

### Compiler Optimization Hoisting

If you attempt to write a lock-free queue using non-atomic variables (e.g., standard `size_t` for indices) and use inline assembly barriers to force ordering, the compiler's optimizer can still hoist reads out of spin-loops.

The compiler assumes that if a variable is not modified by the current thread, its value cannot change. Consequently, it may load the index into a CPU register once and reuse that register inside the loop, leading to an infinite spin-loop. Using `std::atomic` prevents this hoisting optimization.

### The Danger of Spin-Waiting in User-Space

In low-latency pipelines, threads spin-wait (busy-wait) on empty queues instead of sleeping, maintaining high responsiveness. However, spinning at 100% CPU usage causes the processor to consume maximum power, triggering thermal throttling that lowers the CPU clock frequency.

To prevent this, spin-loops should incorporate a `PAUSE` instruction on x86 or a `YIELD` instruction on ARM inside the loop. The `PAUSE` instruction delays execution for a brief window, reducing power consumption and preventing pipeline flushes when the loop condition is finally satisfied.