---
layout: post
title: "Automated Least-Privilege IAM Role Generation for AWS ECS Tasks Using eBPF Auditing"
date: 2026-07-01 08:00:00 +0700
tags: [aws, ebpfs, ecs, security, iam, devsecops]
description: "How to use eBPF to trace containerized AWS SDK calls and dynamically synthesize least-privilege IAM policies, eliminating wildcard permissions."
image: "https://picsum.photos/seed/5350/1080/720"
thumbnail: "https://picsum.photos/seed/5350/400/300"
---

In high-throughput microservices running on AWS Elastic Container Service (ECS), developers often resort to wildcard IAM policy statements like `s3:*` or `kms:*` because manually auditing every single API request, bucket path, and KMS key is a tedious process that inevitably slows down engineering velocity. This developer laziness creates a massive security vulnerability: if a single remote code execution (RCE) vulnerability or dependency supply chain compromise occurs in a container, the attacker immediately queries the ECS task metadata endpoint at `169.254.170.2` to retrieve the temporary credentials of the ECS Task Role. With wildcard permissions, the attacker has instant read-and-write access to your entire database, S3 data lake, and decryption keys. By moving our security auditing down into the Linux kernel using eBPF (Extended Berkeley Packet Filter), we can dynamically intercept decrypted user-space TLS traffic to AWS endpoints directly at the `libssl.so` interface. This allows us to observe every single AWS SDK request made by an ECS task in real-time, trace the exact API action and resource ARN, and automatically synthesize a minimal, least-privilege IAM policy without writing a single line of application code or deploying heavy sidecar proxies.

![Automated Least-Privilege IAM Role Generation for AWS ECS Tasks Using eBPF Auditing Diagram](/images/diagrams/automated-least-privilege-iam-role-generation-aws-ecs-ebpf-auditing.svg)

## The Blind Spots of Traditional IAM Auditing

Traditional approaches to auditing AWS IAM roles in production fail due to high latency, coarse-grained data collection, or extreme performance penalties. 

AWS CloudTrail is the most common tool used to inspect API calls, but it suffers from severe limitations in a DevSecOps feedback loop. First, CloudTrail logs are delivered to S3 with a 5-to-15-minute latency. This makes them useless for real-time CI/CD analysis or integration testing validation. Second, matching CloudTrail API calls back to a specific container or task is highly complex, especially when multiple instances of the same ECS service assume the same IAM role. Third, CloudTrail doesn't capture data-plane operations (like `s3:GetObject` or `s3:PutObject`) unless expensive and verbose object-level logging is enabled. Enabling object-level logging on a high-throughput bucket will double your AWS bill and generate terabytes of log noise.

Static analysis (AST parsing) of codebases tries to solve this by scanning for SDK invocations, such as `s3.GetObjectInput{Bucket: aws.String("my-bucket")}`. However, in production, resource names are rarely hardcoded; they are dynamically injected via environment variables, resolved via configuration managers, or constructed at runtime. Third-party libraries, database drivers, and ORMs also make internal AWS calls that AST parsers fail to catch, resulting in a high rate of false negatives.

VPC Flow Logs operate at Layers 3 and 4, capturing source/destination IPs, ports, and packet volumes. Because AWS APIs run over HTTPS (port 443), VPC Flow Logs see nothing but a stream of encrypted bytes going to AWS IP addresses. They cannot distinguish a critical `kms:Decrypt` operation from a harmless `s3:GetObject` on a public asset.

## Why eBPF is the Answer for Security Auditing

By moving our security auditing down to the kernel using eBPF, we bypass application-level limitations while maintaining near-zero performance overhead. 

To audit TLS-encrypted AWS API traffic without modifying application binaries, we use user-space probes (uprobes) and return probes (uretprobes) to hook into the user-space TLS library of the process (such as `libssl.so.1.1` or `libssl.so.3` on Linux). By intercepting the entry points of `SSL_write` and `SSL_read`, we hook the buffer containing the plaintext HTTP request before OpenSSL encrypts it and sends it to the TCP socket. 

Simultaneously, we attach kernel probes (kprobes) to network system calls like `sys_enter_connect`. This allows us to map the process ID (PID) and socket file descriptors back to the target container's Linux namespaces (net and mount namespaces). This correlation connects the plaintext HTTP request directly to a specific ECS container ID and Task ARN.

Compared to traditional proxying sidecars like Envoy, which intercept network traffic by redirection via iptables and add 2–5ms of latency under heavy load, eBPF runs asynchronously inside the kernel. The eBPF helper copy operations take less than 1.5% CPU overhead under a load of 40,000 requests per second, and have zero impact on the application's request-response latency.

## Writing the eBPF Program to Intercept SSL Buffer

The kernel space eBPF program hooks into the `SSL_write` function. When the application code calls `SSL_write(SSL *ssl, const void *buf, int num)`, the eBPF program intercepts the arguments, extracts the process identity, and copies a slice of the plaintext payload from the user-space buffer into a shared BPF ring buffer.

The following C code shows the implementation of the eBPF uprobe.

<script src="https://gist.github.com/mohashari/56ed960ebfcadc6ec2a3c9b1431e46b2.js?file=snippet-1.txt"></script>

## Designing the User-space Go Agent to Parse AWS API Calls

The user-space agent, written in Go, consumes raw events from the BPF ring buffer. Because the eBPF probe captures the initial bytes of the HTTP payload, the Go agent can parse the stream using standard HTTP parsing libraries. 

AWS services expose RESTful interfaces (like S3) or JSON-RPC endpoints (like DynamoDB, KMS, and STS). For RESTful services, we extract the HTTP method and request path. For JSON-RPC services, we extract the `X-Amz-Target` header, which contains the target API version and action name (e.g., `TrentService.Decrypt`).

The agent decodes this information and outputs a structured log of AWS API events:

<script src="https://gist.github.com/mohashari/56ed960ebfcadc6ec2a3c9b1431e46b2.js?file=snippet-2.go"></script>

## Synthesizing Least-Privilege IAM Policies

Once the integration test suite or staging environment run is complete, we aggregate and deduplicate the collected AWS access events. 

To generate a valid IAM policy, we compile these unique access actions and resource patterns into a unified JSON format, avoiding policy size limit errors (20KB for role policies) by grouping similar resources.

<script src="https://gist.github.com/mohashari/56ed960ebfcadc6ec2a3c9b1431e46b2.js?file=snippet-3.go"></script>

## Automated Enforcement via GitOps and Terraform

To run this pipeline without manual intervention, we deploy the eBPF agent into our local staging environments or CI runners. When integration tests run, the agent sniffs all network calls. Once tests finish successfully, the output JSON policy file is generated and stored directly within the service repository. 

Our Terraform modules then dynamically consume this policy file and attach it directly to the targeted ECS Task Definition.

<script src="https://gist.github.com/mohashari/56ed960ebfcadc6ec2a3c9b1431e46b2.js?file=snippet-4.hcl"></script>

To enforce least-privilege at the pull request level, we write a CI workflow that runs the service test suite, triggers the auditing daemon, and matches the output against the committed version. If a developer introduces a new S3 call but fails to update the policy, the pipeline fails, preventing credential authorization errors in production.

<script src="https://gist.github.com/mohashari/56ed960ebfcadc6ec2a3c9b1431e46b2.js?file=snippet-5.yaml"></script>

## Real-World Production Challenges & Edge Cases

While this automated flow solves the friction of generating least-privilege IAM policies, running this pipeline in production reveals several technical challenges that you must design around.

### OpenSSL Dynamic Linking and Go Static Binaries
Uprobes hook into dynamic symbols. If you compile your backend application using Go or Rust with static linking (e.g., Go's native `crypto/tls` library instead of OpenSSL), the application binary does not link against `libssl.so`. The uprobes attached to `SSL_write` will remain silent. 

To trace native Go binaries, your eBPF program must hook the compiled symbols of the Go runtime itself, such as `crypto/tls.(*Conn).Write`. This requires checking the ELF symbol table of the application binary inside the CI pipeline and passing the resolved offset addresses to the eBPF loader.

### Dynamic Resource Names
If your application writes logs to S3 using UUIDs or timestamped paths (e.g., `s3://my-bucket/logs/2026/07/01/file.txt`), the eBPF analyzer will log unique paths for every call. If written directly to the policy, you will exceed the IAM policy size limit within hours. 

To solve this, the generator logic must implement regex classifiers that automatically generalize dynamic paths to wildcard patterns like `arn:aws:s3:::my-bucket/logs/*` when multiple sequential writes are detected within the same prefix.

### Task Execution Role vs. Task Role Separation
AWS ECS tasks use two distinct roles:
1. **Task Execution Role**: Used by the ECS agent to pull container images, set up logging, and retrieve secrets from AWS Secrets Manager.
2. **Task Role**: Used by the application inside the container to make AWS API calls.

To prevent auditing the wrong permissions, your eBPF filter must isolate namespaces. The eBPF C code should filter calls using the mount namespace ID of the application container (`bpf_get_current_pid_tgid`), ignoring the parent ECS agent daemon process context.

### PrivateLink and Custom VPC Endpoints
If your ECS tasks connect to AWS services through VPC Endpoints (PrivateLink), your packets bypass public DNS. However, because the HTTP `Host` header remains the same (e.g., `s3.us-east-1.amazonaws.com`), the payload parsing logic remains unaffected. 

If you attempt to write a passive monitor that relies on tracing outbound IP blocks alone, PrivateLink will break your routing database. Relying on HTTP-layer headers rather than IP ranges is essential for reliable cloud auditing.

## Conclusion

Automating least-privilege IAM role generation using eBPF takes the guesswork out of DevSecOps. By capturing the actual interactions of ECS containers directly at the kernel-user boundary, you eliminate wildcard policies without adding latency or proxy overhead to your staging or production microservices. Integrating these generated policy templates directly into your Terraform and GitOps configurations allows you to enforce strict data boundaries on every pull request, drastically reducing your cloud infrastructure's attack surface.