---
layout: post
title: "Automating Ephemeral SPIFFE/SPIRE ID Attestation for AWS Fargate Tasks via KMS and OIDC"
date: 2026-07-14 08:00:00 +0700
tags: [devsecops, spiffe, aws-fargate, cryptography]
description: "Bootstrap identity securely in serverless container environments using AWS KMS asymmetric signing and OIDC web identity tokens."
image: "https://picsum.photos/seed/5988/1080/720"
thumbnail: "https://picsum.photos/seed/5988/400/300"
---

In high-throughput microservice architectures running on AWS Fargate, establishing a zero-trust network posture requires that every container container possesses an ephemeral, cryptographically verifiable identity. The Secure Production Identity Framework for Everyone (SPIFFE), implemented via SPIRE, provides the gold standard for this by issuing short-lived X.509 SVIDs (SPIFFE Verifiable Identity Documents). However, bootstrapping identity on AWS Fargate is notoriously difficult: unlike standard EC2 instances, Fargate tasks lack access to the EC2 Instance Metadata Service (IMDSv2), meaning traditional node attestation mechanisms are unavailable. Brittle alternatives, such as using AWS Secrets Manager to store static bootstrap tokens, introduce major operational risks, while calling AWS STS (`sts:GetCallerIdentity`) at scale quickly hits throttling thresholds (like the standard STS limit of 100 requests per second), resulting in severe container startup failures during rapid auto-scaling events. To solve this Fargate attestation paradox, we must implement an automated, ephemeral attestation flow that combines AWS Key Management Service (KMS) asymmetric signing with OIDC web identity tokens, ensuring that Fargate tasks can securely and autonomously prove their identity to a centralized SPIRE Server at scale without relying on shared secrets or hardcoded credentials.

![Automating Ephemeral SPIFFE/SPIRE ID Attestation for AWS Fargate Tasks via KMS and OIDC Diagram](/images/diagrams/automating-spiffe-spire-id-attestation-aws-fargate-kms-oidc.svg)

## The Fargate Attestation Paradox: Why IMDSv2-style Node Attestation Fails

In traditional VM-based environments, a SPIRE Agent running on an EC2 instance performs node attestation by querying the IMDSv2 endpoint (`http://169.254.169.254/latest/dynamic/instance-identity/document`). This returns a cryptographically signed Instance Identity Document (IID) containing the instance's account ID, region, private IP, and instance ID. The SPIRE Server receives this document, validates the signature against AWS's public certificate, and matches the instance metadata against registration entries to issue a node identity.

When we shift workloads to AWS Fargate, this entire chain of trust breaks down. Fargate abstracts the underlying hypervisor and host instance completely. The container runtime environment only exposes the ECS Task Metadata Endpoint (TMDEv4), which contains metadata like container names, network configurations, and resource limits, but lacks any cryptographic signatures or tokens signed by AWS that can prove the container's execution identity to an external server.

Consequently, a SPIRE Agent running inside a Fargate task cannot fetch an IID. If you attempt to use the SPIRE `aws_iid` node attester, it fails immediately with connection timeouts or HTTP 404 errors when attempting to reach the IMDSv2 address.

AWS ECS does inject temporary IAM credentials into the task container using an OIDC-backed identity token via the `AWS_WEB_IDENTITY_TOKEN_FILE` environment variable. While this token allows the task to assume an IAM Role and interact with AWS services, the SPIRE Server cannot natively use this token to attest the task unless the server is configured as a fully fledged OIDC relying party for the AWS OIDC provider, which introduces complex trust-establishment steps and lacks a localized "proof of possession" for ephemeral keys. Without a dedicated attestation helper that binds an ephemeral public key to the task's identity, any compromised container in the VPC could intercept the token and impersonate the task. This requires a hybrid mechanism where the task uses a localized cryptographic signing operation to prove ownership of its ephemeral key pair.

## The Solution: Asymmetric KMS Signing with OIDC Token Possession

To securely bootstrap identity, we combine asymmetric cryptography with AWS KMS and OIDC. Instead of the SPIRE Server directly querying AWS APIs for every Fargate task startup, we delegate the cryptographic proof of identity to AWS KMS and bind it to the task's IAM role.

The architectural flow works as follows:

1. **Key Generation**: When a Fargate task starts, a lightweight attestation sidecar generates an ephemeral EC (Elliptic Curve) or RSA key pair locally in memory. The private key never leaves the memory space of the container.
2. **Client Assertion Construction**: The sidecar constructs a structured JSON client assertion payload. This payload contains metadata identifying the task (Cluster ARN, Task ARN, creation timestamp) and a hash of the ephemeral public key.
3. **KMS Cryptographic Signing**: The sidecar calls the AWS KMS API (`kms:Sign`) using the task's IAM Role. It sends the hash of the client assertion to be signed by a dedicated asymmetric KMS key (e.g., using `RSA_SIGN_PSS_2048_SHA_256` or `ECDSA_SHA_256`). A strict Key Policy on the KMS key ensures that only IAM roles associated with authorized Fargate services are permitted to execute `kms:Sign`.
4. **Attestation Request**: The sidecar sends the raw client assertion, the KMS-generated signature, the ephemeral public key, and the AWS OIDC token (proving the task's identity) to the SPIRE Server.
5. **SPIRE Server Verification**: The SPIRE Server intercepts the request. Using the OIDC configuration, it validates that the task role matches expectations. It then verifies the KMS signature. Because the KMS public key is public and can be cached by the SPIRE Server, this verification is done locally without calling AWS KMS for every request, avoiding critical rate limits.
6. **Task Validation (Optional Out-of-Band Check)**: For high-security environments, the SPIRE Server can execute a one-time `ecs:DescribeTasks` call to verify the task is currently in a `RUNNING` state, protecting against replayed attestation documents.
7. **SVID Issuance**: Once verified, the SPIRE Server registers the Fargate task's public key, maps the task's metadata to the appropriate SPIFFE ID, and returns the X.509 SVID.

## Implementation Details: Configuring the AWS Infrastructure

To set up this architecture, we must define the KMS key, the task execution role, and the key policy. The key policy is the most critical security boundary: it must enforce that only Fargate task roles from specific workloads can perform the signing operation.

The following Terraform configuration defines the asymmetric KMS key and its restrictive access policy.

<script src="https://gist.github.com/mohashari/320608d8955990760ea2b15cd3678c1c.js?file=snippet-1.hcl"></script>

The SPIRE Server needs permissions to download the KMS public key for signature validation, and optionally, permissions to query ECS task metadata.

<script src="https://gist.github.com/mohashari/320608d8955990760ea2b15cd3678c1c.js?file=snippet-2.json"></script>

## The Sidecar Agent: Generating Ephemeral Keys and Requests

Inside the Fargate task, the attestation sidecar must run before the main application starts, or run as a sidecar that exposes the Workload API. The sidecar generates a key pair, constructs the JSON assertion, and invokes AWS KMS.

The following JSON block represents the structure of the JSON client assertion payload. It binds the task metadata to the ephemeral public key hash.

<script src="https://gist.github.com/mohashari/320608d8955990760ea2b15cd3678c1c.js?file=snippet-3.json"></script>

The Go implementation below shows how the Fargate task sidecar generates a key pair, constructs the assertion, and requests AWS KMS to sign the hash of the assertion.

<script src="https://gist.github.com/mohashari/320608d8955990760ea2b15cd3678c1c.js?file=snippet-4.go"></script>

## The SPIRE Server Plugin: Verifying and Attesting the Identity

On the SPIRE Server side, a Node Attester plugin validates this KMS signature. The SPIRE Server uses its local cache of the KMS key's public key (fetched periodically using `kms:GetPublicKey`) to verify the signature of the assertion payload without incurring the round-trip latency and API rate-limiting of hitting AWS KMS for every task instance.

The Go verification logic below illustrates how the SPIRE Server validates the signature using the cached public key.

<script src="https://gist.github.com/mohashari/320608d8955990760ea2b15cd3678c1c.js?file=snippet-5.go"></script>

Next, configure the SPIRE Server Node Attester plugin. The configuration includes defining the expected KMS key ARN, OIDC provider, and allowed task roles.

<script src="https://gist.github.com/mohashari/320608d8955990760ea2b15cd3678c1c.js?file=snippet-6.hcl"></script>

Finally, map the verified claims to a structured SPIFFE ID template.

<script src="https://gist.github.com/mohashari/320608d8955990760ea2b15cd3678c1c.js?file=snippet-7.yaml"></script>

## Production Considerations, Rate Limits, and Failure Modes

Deploying this architecture in production requires a deep understanding of AWS API limits, caching strategies, and potential failure modes to prevent service-to-service communication outages.

### 1. AWS KMS Asymmetric Request Quotas & Throttling
Unlike symmetric key operations, which typically support up to 10,000 requests per second (RPS) out-of-the-box, asymmetric cryptographic operations (such as `kms:Sign` and `kms:Verify`) are subject to much lower default API limits. Depending on the AWS Region, this quota can range from **500 to 2,000 RPS**. 

During a major cluster deployment (e.g., rolling update of a service running 800 tasks), a sudden burst of containers attempting to attest simultaneously can easily trigger `KMSLimitExceededException` (HTTP 400).
* **Mitigation**: You must implement exponential backoff with full jitter in the sidecar's KMS client. Furthermore, the SPIRE Server *must* be configured to verify the RSA signature locally using the cached public key, rather than proxying a `kms:Verify` call back to AWS KMS for every request. This ensures KMS calls are only made once per Fargate task startup.

### 2. AWS ECS DescribeTasks Rate Limits
If the SPIRE Server validates each incoming attestation by calling `ecs:DescribeTasks` (to ensure the Task ARN is actively running and matches the registered cluster), it will quickly run into account-level ECS API throttling. The default limits for ECS read APIs are often as low as **20 to 100 RPS**.
* **Mitigation**: Do not rely on synchronous `DescribeTasks` calls in the critical path of the SPIRE Server's attester for every workload SVID rotation. SVID rotations happen periodically (e.g., every 30 minutes). Instead, leverage the AWS OIDC token's short lifetime. If an out-of-band check is required, build a local cache of active tasks on the SPIRE Server using an AWS EventBridge event stream that listens for ECS Task State Changes, updating an in-memory cache of running Task ARNs.

### 3. Container Clock Skew
Asymmetric signature verification relies on accurate timestamp validation (`iat` and `exp` claims in the assertion payload). While Fargate host instances are synchronized via Amazon Time Sync Service, container instances can experience transient clock drift or synchronization issues during high CPU steal events on the host hypervisor. A clock drift of even **60 seconds** can cause attestation requests to be rejected as expired or not yet valid.
* **Mitigation**: Configure the SPIRE Server node attester plugin to allow a configurable clock skew leeway (e.g., 90 seconds) when validating the `iat` and `exp` claims.

### 4. OIDC Token Expiry and Rotation
AWS rotations for OIDC web identity tokens occur automatically. The injected token at `AWS_WEB_IDENTITY_TOKEN_FILE` has a default lifetime of 24 hours. If a Fargate container is extremely long-lived, the token will be updated on the container's file system by the ECS Agent.
* **Mitigation**: The sidecar agent must not load the token once at startup. Instead, it must read the token file from disk dynamically before performing any new attestation request to ensure it always presents a valid, unexpired token to the SPIRE Server.