---
layout: post
title: "Writing a Custom Kubernetes Scheduler Plugin in Go to Co-Locate Microservices with Low-Latency Shared Memory IPC"
date: 2026-08-24 08:00:00 +0700
category: open_source_community_and_cncf_projects
tags: [kubernetes, golang, performance, ipc, systems-programming]
description: "Learn how to bypass network stack overhead by writing a custom Go-based Kubernetes scheduler plugin that co-locates microservices for sub-microsecond shared memory IPC."
image: "/images/diagrams/writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.svg"
thumbnail: "/images/diagrams/writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.svg"
---

When you are operating high-throughput telemetry pipelines, financial order books, or real-time media processing engines in Kubernetes, the kernel's network stack quickly becomes your primary performance bottleneck. Even with highly optimized gRPC over HTTP/2, loopback TCP connections between co-located containers incur socket buffer allocations, checksum calculations, context switches, and IP routing table lookups that push p99 latencies well past 1.5 milliseconds under heavy load. Unix Domain Sockets reduce this latency, but they still require kernel-space context switching and system-call copies. To achieve sub-microsecond, zero-copy IPC, microservices must share raw physical memory. However, Kubernetes does not guarantee that separate pods will land on the same physical worker node, and default scheduler affinity rules carry severe CPU and queue-depth scaling overhead at scale. This article details how to build, compile, and run a custom Kubernetes scheduler plugin in Go that reliably co-locates microservices on the same node to unlock raw shared-memory IPC, bypassing the network stack entirely.

![Writing a Custom Kubernetes Scheduler Plugin in Go to Co-Locate Microservices with Low-Latency Shared Memory IPC Diagram](/images/diagrams/writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.svg)

## The Performance Wall: Loopback TCP vs. Shared Memory IPC

At a high request volume (e.g., 500,000 requests per second), loopback TCP socket operations consume substantial CPU time inside kernel functions like `tcp_v4_rcv` and `ip_local_deliver`. Every packet transferred requires copying data from user space to kernel space in the sender process, and then from kernel space to user space in the receiver. This dance triggers hardware interrupts, softirqs, and frequent context switches. 

The table below contrasts loopback TCP, Unix Domain Sockets, and POSIX Shared Memory IPC:

| Metric | TCP Loopback (HTTP/2) | Unix Domain Sockets | Shared Memory (POSIX /dev/shm) |
| :--- | :--- | :--- | :--- |
| **Typical Latency** | 120µs - 500µs | 15µs - 45µs | 150ns - 450ns |
| **Data Copy Operations** | 2 (User -> Kernel -> User) | 1 (User -> Kernel -> User via pipe) | 0 (Zero-copy memory mapping) |
| **Syscall Overhead** | High (`write`, `read`, `epoll_wait`) | Medium (`write`, `read`) | None (Direct memory access after `mmap`) |
| **CPU Utilization** | High (Network stack traversal) | Moderate (Kernel buffer copying) | Extremely Low (CPU cache lines only) |

Shared memory IPC relies on the [`mmap`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L150) system call to map the same physical RAM page into the virtual address spaces of both processes. Once the mapping is initialized, communication bypasses the kernel entirely. A write operation is reduced to a simple assembly `mov` instruction, and synchronization is handled via atomic operations in user space. 

However, mapping memory between separate containers requires them to reside on the same physical host and share a backing memory-mapped file system (typically a `tmpfs` mount under `/dev/shm`). If the Kubernetes scheduler places these pods on different nodes, the pods must fall back to network-based communication, immediately introducing latency spikes and breaking real-time SLAs.

## Why Default Kubernetes Scheduling Falls Short

Kubernetes provides `podAffinity` to enforce co-location. For example, you can specify that `Pod-B` has a hard affinity requirement to run on a node hosting `Pod-A`. While this works for simple setups, it introduces critical limitations in production:

1. **Scheduling Complexity and Latency ($O(P \times N)$):** The default scheduler evaluates `podAffinity` by iterating over every running pod in the cluster to check if its labels match the selector. In clusters with tens of thousands of pods ($P$) and thousands of nodes ($N$), this results in an $O(P \times N)$ time complexity. This label-matching overhead blocks the scheduler's queue, degrading scheduling throughput from 100+ pods per second to single digits.
2. **Lack of Resource Allocation Mapping:** Co-location is only part of the equation. If both pods write to a 2GB shared memory segment, the scheduler must ensure that the target node has enough unallocated physical memory. Standard pod memory limits do not track memory mapped via `hostPath` or `tmpfs` cleanly, leading to silent memory exhaustion.
3. **Absence of Strict Paired Topologies:** `podAffinity` is generic. It cannot enforce that exactly one producer and exactly one consumer are scheduled per node. Without coordination, the scheduler might pack ten consumers onto Node A and ten producers onto Node B, rendering shared-memory IPC unusable.

To solve these constraints, we can use the **Kubernetes Scheduling Framework**. This framework allows developers to write custom plugins in Go, compile them directly into the `kube-scheduler` binary, and execute custom scheduling logic at key stages of the scheduling cycle.

## Anatomy of the Co-Location Scheduler Plugin

The custom scheduler plugin, which we will name [`SHMCoLocate`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L12), intercepts the scheduling cycle at three extension points:

* **[`PreFilter`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L30):** Extracts the target co-location group ID from the pod's annotations. If the annotation is present, it caches this value within the thread-safe [`CycleState`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L32) context to avoid parsing annotations repeatedly in subsequent phases.
* **[`Filter`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L45):** Identifies if a pod belonging to the same group ID has already been scheduled to a node. If an active node is found, all other nodes in the cluster are filtered out.
* **[`Score`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L80):** Prioritizes nodes with higher allocatable physical RAM capacity to ensure that the node's page cache can comfortably handle the shared memory maps without triggering the out-of-memory (OOM) killer.

## Implementing the Custom Scheduler Plugin in Go

Let's implement the scheduling plugin using the Kubernetes scheduling framework APIs. We will organize the implementation into several Go modules.

First, we define the base structure of our plugin and register its factory method in the scheduling framework.

<script src="https://gist.github.com/mohashari/9f45a2e95e782039ed3a79892a656fac.js?file=snippet-1.go"></script>

Next, we implement the [`PreFilter`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L30) interface. This phase determines if the incoming pod participates in shared memory co-location. It parses the custom annotation and stores the group identifier in [`CycleState`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L32).

<script src="https://gist.github.com/mohashari/9f45a2e95e782039ed3a79892a656fac.js?file=snippet-2.go"></script>

Now we implement the [`Filter`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L45) phase. In this step, the plugin lists existing pods and evaluates if any pod with the same group ID has already been scheduled. If a group member has been assigned to a node, this node becomes the only valid candidate.

<script src="https://gist.github.com/mohashari/9f45a2e95e782039ed3a79892a656fac.js?file=snippet-3.go"></script>

Finally, we implement the [`Score`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L80) phase. Ranting nodes by allocatable memory guarantees that the scheduler prefers robust nodes with high physical RAM headroom, reducing the chance of page cache contention.

<script src="https://gist.github.com/mohashari/9f45a2e95e782039ed3a79892a656fac.js?file=snippet-4.go"></script>

## Compiling and Registering the Custom Scheduler

To run this plugin, we must wrap it in a custom main entrypoint that initializes the standard Kubernetes scheduler command with our plugin registered.

<script src="https://gist.github.com/mohashari/9f45a2e95e782039ed3a79892a656fac.js?file=snippet-5.go"></script>

To compile this custom binary, use a standard Go build command target. Next, configure the scheduler configuration YAML to enable our custom extension points inside the default active profile:

```yaml
# snippet-6
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
leaderElection:
  leaderElect: true
  resourceName: shm-scheduler
  resourceNamespace: kube-system
profiles:
  - schedulerName: shm-scheduler
    plugins:
      preFilter:
        enabled:
          - name: "SHMCoLocate"
      filter:
        enabled:
          - name: "SHMCoLocate"
      score:
        enabled:
          - name: "SHMCoLocate"
```

This configuration is typically injected into the scheduler binary via a ConfigMap and mounted into the control plane pod manifest.

## Mounting and Consuming Shared Memory

Since we are running separate pods to preserve isolation boundaries, independent lifecycles, and discrete role permissions, we cannot use a standard Pod-level `emptyDir` memory volume. Instead, both pods mount a node-level `/dev/shm` subdirectory using a `hostPath` volume. 

The custom scheduler forces both pods onto the same node, ensuring that the local file paths map to the exact same physical memory segments.

```yaml
# snippet-7
apiVersion: v1
kind: Pod
metadata:
  name: telemetry-producer
  namespace: data-pipeline
  labels:
    role: producer
  annotations:
    shm-co-locate-group.alpha.kubernetes.io/id: "pipeline-shm-group-0"
spec:
  schedulerName: shm-scheduler
  containers:
    - name: writer
      image: registry.yourorg.com/telemetry/writer:v2.1.0
      volumeMounts:
        - name: shm-ipc
          mountPath: /dev/shm
  volumes:
    - name: shm-ipc
      hostPath:
        path: /dev/shm/groups/pipeline-shm-group-0
        type: DirectoryOrCreate
---
apiVersion: v1
kind: Pod
metadata:
  name: telemetry-consumer
  namespace: data-pipeline
  labels:
    role: consumer
  annotations:
    shm-co-locate-group.alpha.kubernetes.io/id: "pipeline-shm-group-0"
spec:
  schedulerName: shm-scheduler
  containers:
    - name: reader
      image: registry.yourorg.com/telemetry/reader:v2.1.0
      volumeMounts:
        - name: shm-ipc
          mountPath: /dev/shm
  volumes:
    - name: shm-ipc
      hostPath:
        path: /dev/shm/groups/pipeline-shm-group-0
        type: DirectoryOrCreate
```

## Writing the Zero-Copy IPC Go Application

With the scheduler guaranteeing co-location, the Go applications in both containers can access the shared memory directory. We write a Go program that creates a backing file, truncates it to size, maps it via [`syscall.Mmap`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L150), and executes zero-copy memory reads and writes using raw pointers.

<script src="https://gist.github.com/mohashari/9f45a2e95e782039ed3a79892a656fac.js?file=snippet-8.go"></script>

## Real-World Production Traps and Mitigation

Deploying custom scheduler plugins and raw memory mapping in production exposes edge cases that you must address to prevent outages.

### 1. Rolling Updates and Scheduler Deadlocks
During a rolling update of a co-located deployment, the scheduler creates a new instance of the producer pod (`Producer-New`). If the node hosting the active `Consumer` pod is fully utilized, the scheduler cannot place `Producer-New` on that node due to resource limits. At the same time, the scheduler cannot schedule `Producer-New` on any other node because the `Consumer` pod is still anchored to the original node. The update deadlocks, leaving the pipeline hung.

* **Mitigation:** Implement a fallback check in the [`Filter`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L45) phase. If the only anchored node has insufficient resources to schedule the incoming pod, the plugin must permit scheduling on a new node and write an eviction signal to the old node's namespace. Alternatively, enforce the `Recreate` deployment strategy instead of `RollingUpdate`, ensuring the old pods are fully terminated before new ones are scheduled.

### 2. Node-Level Memory Over-Commitment (Silent OOMs)
Memory mapped via `/dev/shm` is backed by `tmpfs` (RAM). Unlike standard container processes, memory-mapped allocations via `hostPath` are not tracked under container cgroup limits. Kubelet remains unaware of these allocations. If the processes write heavily to their shared pages, they can exceed the physical memory capacity of the host, triggering a kernel OOM event that kills system processes like `kubelet` or `containerd`.

* **Mitigation:** Configure a strict size boundary on the host path mount inside the pods. Enforce a node allocation safety buffer in your [`Score`](file:///home/muklis/Documents/exploring/blog/_posts/2026-08-24-writing-custom-kubernetes-scheduler-plugin-go-co-locate-microservices-shared-memory-ipc.md#L80) plugin, preventing the placement of high-memory IPC pods on nodes running with less than 20% free physical memory headroom.

### 3. Dirty Memory States on Pod Crashes
If the producer pod crashes mid-write while holding a user-space spinlock inside the shared memory structure, the consumer pod will wait indefinitely on the lock, causing a pipeline hang. Additionally, the new producer pod will attempt to map the existing memory segment, encountering corrupted data.

* **Mitigation:** Avoid blocking locks in shared memory. Instead, use lock-free data structures using atomic index swaps (`sync/atomic`). If locking is required, implement robust POSIX mutexes (`PTHREAD_MUTEX_ROBUST`) in your application layer. These mutexes notify waiting processes if the owner process terminates while holding the lock, allowing the survivor to recover and clean up the shared state.