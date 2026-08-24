---
layout: post
title: "Mitigating Side-Channel Attestation Leaks: Hardening AWS KMS Key Policies for Multi-Tenant EKS Workloads"
date: 2026-08-24 08:00:00 +0700
tags: [eks, devsecops, aws-kms, kubernetes, security]
description: "Hardening multi-tenant EKS clusters against side-channel attestation leaks using AWS KMS key policies, strict IRSA OIDC trust, and cryptographic context."
image: "https://picsum.photos/seed/2447/1080/720"
thumbnail: "https://picsum.photos/seed/2447/400/300"
---

In multi-tenant Amazon Elastic Kubernetes Service (EKS) clusters, isolating workloads at the namespace level is a standard operational baseline. Platform teams routinely implement Kubernetes NetworkPolicies, configure container security contexts, and segment service accounts to enforce logical boundaries. However, a systemic vulnerability often goes unnoticed at the intersection of Kubernetes namespaces and the AWS cloud control plane. If a microservice in namespace `tenant-a` is compromised via remote code execution, a sophisticated attacker will immediately seek to pivot from the container runtime to the AWS API. By exploiting loosely configured IAM Roles for Service Accounts (IRSA) or abusing shared worker node instance profiles, they can use the AWS control plane as a side-channel to bypass all logical Kubernetes isolation. If your AWS Key Management Service (KMS) key policies rely solely on role-based identity checks without enforcing cryptographic binding parameters, the attacker can silently exfiltrate and decrypt sensitive tenant databases, transaction logs, and secrets. This post analyzes the mechanics of these side-channel attestation leaks and provides concrete, production-tested patterns to harden your AWS KMS key policies and secure multi-tenant EKS workloads.

## The Mechanics of EKS Attestation Exploits

To understand how side-channel attestation leaks occur, we must first examine the authentication handshake between EKS and AWS STS (Security Token Service). Under the hood, IRSA utilizes OpenID Connect (OIDC) federation. Each EKS cluster acts as an OIDC Identity Provider (IdP) generating cryptographically signed JSON Web Tokens (JWTs).

When a pod is associated with a Kubernetes `ServiceAccount` annotated with an IAM role ARN, the EKS Pod Identity Webhook intercepts the pod creation request and injects two critical components:
1. An environment variable `AWS_ROLE_ARN` pointing to the designated IAM role.
2. A projected volume containing a short-lived OIDC token JWT, mounted at `/var/run/secrets/eks.amazonaws.com/serviceaccount/token` and configured via the environment variable `AWS_WEB_IDENTITY_TOKEN_FILE`.

When the application code initializes an AWS SDK client, the SDK automatically reads these variables and executes an `AssumeRoleWithWebIdentity` call to STS, exchanging the Kubernetes-signed JWT for temporary AWS credentials. The STS service validates the signature of the token against the cluster’s OIDC issuer endpoint and checks the target IAM role's trust relationship policy.

The attestation gap arises when trust relationships rely on broad wildcard matchers. For instance, using `system:serviceaccount:tenant-a:*` in the `sub` claim check allows *any* pod running in `tenant-a` to assume the role designed for your most critical database writer. Even worse, generic roles shared across clusters or namespaces using wildcard strings allow cross-tenant token exchange. If an attacker gains execution capabilities within a low-privilege pod, they can extract the projected token from the filesystem, query STS, and obtain the identity of a high-privilege role.

<script src="https://gist.github.com/mohashari/1c94b6428008f7807ec72b917404c91b.js?file=snippet-1.json"></script>

The trust policy shown in **Snippet 1** closes this loophole. By swapping `StringLike` wildcards for an exact `StringEquals` match on the OIDC `sub` claim, we explicitly bind the IAM role to a single namespace (`payment-processing`) and a specific Kubernetes ServiceAccount (`payment-service-sa`). Additionally, pinning the `aud` claim to `sts.amazonaws.com` prevents token replay attacks across non-AWS services.

## Cryptographic Equivalency & The Role Bypass

Even with fully hardened IRSA trust policies, a fundamental vulnerability remains: *cryptographic equivalency*. When you authorize an IAM role to decrypt data via a KMS key policy using the `Principal` element, AWS IAM evaluates the caller's identity at the point of request. 

Consider an EKS cluster where the payment service and the reconciliation service in different namespaces assume the same IAM role to read data from a shared S3 bucket. During the OIDC token exchange, the caller's EKS-specific context (namespace and service account name) is stripped away once STS issues the temporary credentials. As far as AWS KMS is concerned, the caller is simply the IAM Principal `arn:aws:sts::123456789012:assumed-role/EksAppRole/botocore-session-*`. KMS has no native mechanism to inspect the pod or namespace from which the request originated.

If an attacker compromises the reconciliation service, they can read and decrypt the raw payment database dumps because the KMS key policy only verifies the IAM Role ARN. To resolve this, we must employ **KMS Encryption Context**.

Encryption Context is a set of non-secret key-value pairs that are cryptographically bound to the ciphertext as Additional Authenticated Data (AAD). When encrypting data, the application must pass this context. During decryption, the exact same context must be supplied; otherwise, the cryptographic operation fails. By structuring KMS Key Policies to enforce these context parameters via IAM condition keys, we can isolate workloads even when they share the same IAM Principal identity.

<script src="https://gist.github.com/mohashari/1c94b6428008f7807ec72b917404c91b.js?file=snippet-2.json"></script>

In the key policy displayed in **Snippet 2**, the second statement binds cryptographic operations to a mandatory condition. The API request will be blocked immediately by AWS KMS if the caller fails to provide the exact `tenant_id` and `kubernetes.io/namespace` context. The `Null` check is a critical safeguard; it prevents callers from omitting the context parameter entirely to bypass validation.

## Implementing Secure Cryptographic Enveloping in Application Code

Hardening policies is only half the battle. Application code must actively leverage the encryption context during cryptographic operations. Furthermore, we must implement error sanitization. When KMS decryption fails, it returns specific exceptions, such as `InvalidCiphertextException` or `AccessDeniedException`. 

If your backend microservice intercepts these exceptions and propagates them raw to the client-facing APIs, it introduces an information disclosure side-channel. An attacker can probe the endpoint, changing headers or payloads to determine whether a key exists, whether the ciphertext structure is valid, or whether they have hit a policy block.

The following Go implementation demonstrates how to construct a secure envelope client using the AWS SDK for Go v2, bind the transaction context, and sanitize downstream errors to prevent data leaks.

<script src="https://gist.github.com/mohashari/1c94b6428008f7807ec72b917404c91b.js?file=snippet-3.go"></script>

By wrapping the AWS SDK KMS calls in this secure abstraction (**Snippet 3**), you ensure that every developer on your team implicitly complies with the cryptographic context mandate. 

## Automating Multi-Tenant Isolation via Terraform

Deploying tenant-segregated KMS Keys, OIDC roles, and Kubernetes service accounts manually is highly prone to configuration drift. Using Terraform, we can codify this setup, ensuring that every tenant receives a fully isolated key, dedicated IAM role, and namespace service account matching the security parameters.

<script src="https://gist.github.com/mohashari/1c94b6428008f7807ec72b917404c91b.js?file=snippet-4.hcl"></script>

The configuration in **Snippet 4** dynamically enforces that the KMS Key Policy allows operations only if the caller's context contains the correct metadata mapping. This prevents developers from accidentally assigning a shared key to multiple tenants and eliminates manual configuration errors.

## Node-Level Attack Vectors and CSI Hardening

Persistent volume encryption represents another critical vulnerability vector. When workloads use dynamic PVCs backed by the AWS EBS CSI driver, the driver runs as a DaemonSet across your worker nodes, utilizing a dedicated CSI IAM role to manage volumes and create KMS grants.

If a pod is compromised, an attacker might attempt to exploit the node's underlying access capabilities. If the worker nodes share a single EBS KMS key, a pod running with root-equivalent privileges or container escape vulnerabilities can leverage the host’s volume mount points. Even worse, if IMDSv1 is enabled, any container can query the instance metadata service to obtain the node's credentials, bypassing IRSA entirely.

To secure EBS key operations, you must implement the following controls:
1. **Enforce IMDSv2**: Disable IMDSv1 on all EKS node groups and limit the hop count to 1. This prevents containerized workloads from reaching the metadata service because the additional network hop across the virtual ethernet interface (`veth`) drops the packet.
2. **Restrict EBS CSI Grants**: Harden the IAM policy attached to the EBS CSI Driver role. It should only be allowed to request KMS grants for specific customer-managed keys associated with your EKS cluster, rather than wildcard permissions on all account keys.

<script src="https://gist.github.com/mohashari/1c94b6428008f7807ec72b917404c91b.js?file=snippet-5.json"></script>

The IAM policy in **Snippet 5** restricts the CSI driver. By locking down the `kms:ViaService` to `ec2.us-west-2.amazonaws.com` and forcing `kms:GrantIsForAWSResource` to true, we ensure that the driver can only execute KMS operations on behalf of official EC2 mounting operations. The attacker cannot call KMS APIs directly with the driver's role credentials to extract data.

## Auditing and Detecting KMS Anomalies

Security policies are only as good as the auditing processes that validate them. To maintain visibility into your multi-tenant environment, you must actively scan AWS CloudTrail logs for anomalies that indicate potential namespace containment bypasses or brute-force context guessing.

The following SQL query, designed for Amazon Athena, scans CloudTrail records to identify:
* KMS Decrypt calls that failed due to authorization issues (`AccessDenied`) or invalid cryptographic parameters (`InvalidCiphertextException`).
* Requests originating from EKS IRSA roles that did not supply the mandatory `tenant_id` context parameter.

<script src="https://gist.github.com/mohashari/1c94b6428008f7807ec72b917404c91b.js?file=snippet-6.sql"></script>

Running this query (**Snippet 6**) in your automated detection pipeline allows your Security Operations Center (SOC) to flag container-compromise events within minutes. A spike in `InvalidCiphertextException` from a specific IP address indicates that a pod has been compromised and the attacker is attempting to decrypt exfiltrated data by guessing encryption context parameters.

## Production Hardening Checklist

When securing your multi-tenant EKS cluster against cloud-control plane side-channel leaks, ensure that your deployment pipeline implements these five rules:

* **Rule 1: Pin Every IRSA Trust Relationship** – Never use wildcards for the `sub` claim inside IAM role trust policies. Enforce exact service account and namespace mappings.
* **Rule 2: Implement Cryptographic Context Enveloping** – Ensure that all sensitive application data stored in shared locations (S3, RDS, DynamoDB) is encrypted with KMS keys requiring a unique `tenant_id` and `kubernetes.io/namespace` context.
* **Rule 3: Enforce Key Segregation** – Do not share KMS keys across logical tenant boundaries unless absolutely necessary. Maintain separate KMS keys for application payloads and cluster infrastructure (EBS, secrets).
* **Rule 4: Disable IMDSv1 and Restrict Hop Limits** – Set the metadata HTTP tokens to `required` (IMDSv2 only) and set the response hop limit to `1` across all EKS node groups.
* **Rule 5: Sanitize Decryption Errors** – Ensure application wrapper code intercepts raw KMS failure exceptions, returning generic errors to clients to prevent context discovery side-channels.