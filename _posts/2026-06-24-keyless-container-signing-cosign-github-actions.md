---
layout: post
title: "Hardening Build Pipelines with Keyless Container Image Signing Using Cosign and GitHub Actions"
date: 2026-06-24 08:00:00 +0700
tags: [devsecops, supply-chain-security, github-actions, kubernetes, cosign]
description: "Eliminate long-lived signing keys and secure your software supply chain using Sigstore's keyless signing workflow with Cosign and GitHub Actions."
image: "https://picsum.photos/seed/keyless/1080/720"
thumbnail: "https://picsum.photos/seed/keyless/400/300"
---

At 3:14 AM, a push notification alerts your on-call engineer that an anomalous container image has been deployed to your production Kubernetes cluster. The image is tagged `release-v2.1.4`, but its binary digest doesn't match anything built by your official CI/CD pipeline. Your registry credentials had been compromised via a developer's leaked personal access token, allowing an attacker to push a backdoored image directly to your registry and bypass git reviews. If your cluster had been verifying container image signatures tied to ephemeral workload identities, that deployment would have been blocked at the admission control gate. Traditional container signing relied on long-lived GPG or Cosign keys stored in secrets managers—a practice that merely moves the compromise target from the registry to the secrets manager. The real solution is keyless signing, where trust is anchored in ephemeral, OIDC-backed credentials verified in real-time.

## The Fallacy of Long-Lived Cryptographic Secrets

Traditional code and container signing relies on persistent key pairs. In a standard setup, you generate a private key using GPG or `cosign generate-key-pair`, store it in a secrets manager (such as Vault, AWS Secrets Manager, or GitHub Secrets), and distribute the public key to your verification environments. 

While mathematically sound, this model fails in real-world production environments for several reasons:

1. **Key Leakage and Compromise:** Secrets stored in CI/CD variables are exposed to compromise via runner exploits, misconfigured logs, build-time dependency attacks, or insider threats. If a private key is leaked, an attacker can sign arbitrary malicious artifacts, rendering the entire cryptographic chain of trust worthless.
2. **Rotation Overhead:** Rotating cryptographic keys is notoriously difficult. It requires updating the key in the build pipeline, distributing the new public key to all verification engines, and managing the transition period where older, legitimately signed images must still be verified using the old key. Consequently, rotation is rarely done, leaving keys active for years.
3. **Lack of Non-Repudiation:** A key signature only proves that whoever possessed the private key signed the artifact. It does not cryptographically bind the signing action to a specific runner, workflow run, or developer identity. If a key is shared across multiple projects, attributing a signature to a specific build pipeline is impossible.

Keyless signing—pioneered by the Sigstore project—replaces persistent private keys with short-lived, ephemeral certificates. By leveraging OpenID Connect (OIDC) identity federation, the build runner proves its identity to an ephemeral Certificate Authority (CA) which issues a certificate valid for only a few minutes. The runner signs the container image, publishes the signature and certificate to a public ledger, and discards the private key. There are no keys to store, rotate, or leak.

## Architecture of OIDC-Backed Keyless Signing

The keyless signing flow utilizes three core components of the Sigstore ecosystem: Fulcio, Rekor, and your platform's OIDC Provider (in this case, GitHub Actions).

```
+------------------+         1. Request OIDC Token         +-----------------------+
|  GitHub Actions  |-------------------------------------->| GitHub OIDC Provider  |
|  Runner (Workload|                                       |                       |
|     Identity)    |<--------------------------------------|                       |
+------------------+          2. OIDC JWT Issued           +-----------------------+
         |
         | 3. Send OIDC JWT & Ephemeral Public Key
         v
+------------------+
|  Fulcio (CA)     |
|                  |---\ 4. Validate JWT Claims
|                  |<--/
|                  |-----------------------------------\
|                  | 5. Issue Ephemeral Certificate     |
+------------------+ (10-min validity, binds identity)  |
         |                                              v
         | 6. Sign Container Digest with                |
         |    Ephemeral Private Key                     |
         v                                              v
+------------------+                            +------------------+
| Container Registry|                            |  Rekor Log       |
| (GHCR/ECR/GCR)   |                            | (Transparency)   |
|                  |                            |                  |
|  [Pushes Image]  |                            | [Records Cert    |
|  [Pushes Sign]   |                            |  & Signature]    |
+------------------+                            +------------------+
```

The process unfolds in six distinct cryptographic operations:

1. **OIDC Token Request:** The GitHub Actions runner requests a short-lived OIDC JSON Web Token (JWT) from GitHub's token authority. This token contains metadata about the execution environment, including the repository name, workflow filename, run ID, and Git ref.
2. **Key Pair Generation:** The runner generates an ephemeral asymmetric cryptographic key pair in-memory. The private key never touches the disk and is never exposed outside the memory space of the signing process.
3. **Certificate Request:** The runner sends the OIDC JWT and the ephemeral public key to **Fulcio**, Sigstore's free, root-of-trust Certificate Authority.
4. **Validation and Issuance:** Fulcio validates the OIDC JWT signature using GitHub's public keys. It extracts the workload identity (the email-like subject representing the workflow runner) and issues a short-lived X.509 certificate. This certificate binds the public key to the workload identity and is given a Time-To-Live (TTL) of exactly 10 minutes.
5. **Signing:** The runner signs the container image's SHA256 digest using its ephemeral private key.
6. **Transparency Log Entry:** The runner submits the signature, the Fulcio certificate, and the artifact metadata to **Rekor**, Sigstore's append-only, cryptographic transparency ledger. Rekor writes this entry to its Merkle tree, returning a Signed Entry Timestamp (SET).
7. **Cleanup:** The runner discards the ephemeral private key. The signature and the Fulcio certificate are uploaded to the OCI registry as a detached artifact attached to the target image tag.

When a client verifies the image later, it checks the signature against the public key in the Fulcio certificate, verifies that the certificate was valid at the precise time recorded in Rekor's signed tree, and checks that the certificate's subject matches the expected GitHub Actions workflow identity.

## Step-by-Step GitHub Actions Workflow Integration

To implement keyless signing in GitHub Actions, you must explicitly configure the runner permissions. Specifically, the runner must have `id-token: write` permissions to retrieve the OIDC JWT from GitHub's OIDC provider.

Here is a production-grade GitHub Actions workflow that builds a container, pushes it to GitHub Container Registry (GHCR), and signs the digest keylessly.

<script src="https://gist.github.com/mohashari/145d651fa4e0565e065f26da98fcdd11.js?file=snippet-1.yaml"></script>

### Critical Operational Details of the Workflow

- **`id-token: write`**: This permission allows the job to call the GitHub OIDC provider API and fetch a JWT containing the details of the active workspace and workflow execution.
- **Sign by Digest, Not Tag**: In the final step, we reference the container image using its immutable SHA256 digest (`${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}@${IMAGE_DIGEST}`) rather than its tag. Container registries allow tag overwrites. If you sign a tag (e.g., `:v1.2.0`), an attacker with write access to the registry can push a malicious image and re-point the tag to it. Verification would fail because the signature doesn't match the new digest, but if you sign the digest directly, the signature is cryptographically bound to that exact build artifact.
- **`--yes` Flag**: By default, Cosign prompts the operator to confirm keyless signing via an interactive browser flow. The `--yes` flag tells Cosign to run in non-interactive mode and use the ambient environment's OIDC token automatically.
- **Annotations (`-a`)**: Adding annotations (like `repo`, `run_id`, and `sha`) embeds metadata into the signed payload. These annotations are visible in the signature payload and can be used for advanced admission control policies.

## Hardening Verification: Don't Just Sign, Enforce

Signing an image is only half the battle. If your deployment environment pulls and runs unsigned images, you have achieved nothing. The first step in verification is manual or CLI-based verification during deployment or inside local developer environments.

To verify a keylessly signed container image, you must specify the expected OIDC issuer and the expected identity subject. Without these flags, Cosign will verify that the image was signed by *someone* using Sigstore, but not that it was signed by *your* pipeline.

<script src="https://gist.github.com/mohashari/145d651fa4e0565e065f26da98fcdd11.js?file=snippet-2.sh"></script>

The output printed by `cosign verify` contains the workload identity subject and token issuer, which you must cross-reference against your repository layout to prevent unauthorized builds from being accepted.

## Kyverno Cluster Policy for Kubernetes Admission Control

For Kubernetes environments, manual verification is not viable. You must implement a validating admission controller that inspects container images prior to scheduling them onto nodes. Kyverno is an excellent tool for this task, as it provides native Sigstore integration.

The following Kyverno `ClusterPolicy` intercepts pod creation requests in production namespaces and enforces that any container image pulled from your organization's registry is signed keylessly by your specific GitHub repository.

<script src="https://gist.github.com/mohashari/145d651fa4e0565e065f26da98fcdd11.js?file=snippet-3.yaml"></script>

### Key Policy Components

- **`mutateDigest: true`**: This is a critical security control. When a user applies a manifest containing an image reference like `ghcr.io/mohashari/blog-app:v1.0.0`, Kyverno queries the registry, resolves the tag to its immutable digest, verifies the signature, and rewrites the Pod specification in-flight to use the digest. This prevents "Time of Check to Time of Use" (TOCTOU) attacks, where the image tag is updated between the admission phase and the kubelet's container runtime pull phase.
- **`webhookTimeoutSeconds: 15`**: Resolving OCI images and checking the Rekor transparency log requires network calls. If the default Kyverno timeout (3 seconds) is reached, the validation may fail closed, blocking deployments. Extending the timeout to 15 seconds ensures resilience against latency spikes in public log servers.

## Multi-Stage Verification and SBOM Attestation

Supply chain security is not limited to image signatures. A secure build pipeline must also verify that the code has been scanned for vulnerabilities and that its software bill of materials (SBOM) matches the deployed artifact. 

Using Syft and Cosign, we can generate a standardized SBOM in SPDX format, attach it to our OCI image as an attestation, and sign the attestation keylessly.

<script src="https://gist.github.com/mohashari/145d651fa4e0565e065f26da98fcdd11.js?file=snippet-4.yaml"></script>

We can subsequently configure Kyverno to verify that a signed SBOM attestation exists before allowing a container to run in production.

<script src="https://gist.github.com/mohashari/145d651fa4e0565e065f26da98fcdd11.js?file=snippet-5.yaml"></script>

## Production Edge Cases and Real-World Failure Modes

Transitioning to keyless signing is not without operational challenges. Below are the primary failure modes you will encounter in high-throughput environments and how to remediate them.

### 1. Rekor and Fulcio Rate Limiting
The public Sigstore infrastructure is shared across the global developer community. If your build runners execute hundreds of container builds and signatures every hour, you may experience HTTP `429 Too Many Requests` or write timeouts from the public Rekor instance.

**Remediation:** 
For massive scale, deploy private instances of the Sigstore stack. For most workloads, configuring retry mechanisms inside the GitHub Actions runner is the simplest solution:

<script src="https://gist.github.com/mohashari/145d651fa4e0565e065f26da98fcdd11.js?file=snippet-6.sh"></script>

### 2. Clock Drift on Self-Hosted Runners
Fulcio certificates have a lifetime of only 10 minutes. If your CI/CD pipelines run on self-hosted VMs whose clocks drift by more than 5 minutes from Fulcio's root system clocks, the validation certificate will be rejected or expire mid-transaction, generating `certificate has expired` errors.

**Remediation:**
Ensure NTP (`chronyd` or `systemd-timesyncd`) is configured and active on all runner host servers. Check synchronization status inside your runner provisioning scripts:

<script src="https://gist.github.com/mohashari/145d651fa4e0565e065f26da98fcdd11.js?file=snippet-7.sh"></script>

### 3. Registry Authentication and RBAC Controls
Unlike traditional signing where the signature is a static file, Cosign pushes signatures directly to the OCI registry as additional image layers (`sha256-<digest>.sig`). 

Your runner's `${{ secrets.GITHUB_TOKEN }}` has package write permissions, but your Kubernetes nodes pulling images using a pull-only registry credential will also need access to read the signature layers. Ensure that your registry RBAC controls and IAM policies allow `ecr:BatchGetImage` and `ecr:GetDownloadUrlForLayer` (for AWS ECR) on both the images and the detached signature files.

## Conclusion: Moving Towards Zero-Trust Software Supply Chains

Traditional methods of securing software pipelines—primarily reliant on network boundaries and long-lived static secrets—are insufficient to combat modern supply chain exploits. Implementing OIDC-backed keyless signing represents a paradigm shift: it transitions your pipeline security model from key-based trust to identity-based trust.

By integrating Cosign keyless signing, GitHub Actions OIDC federation, and Kyverno admission controls, you build a closed-loop delivery pipeline. Unsigned, untrusted, or un-audited container images are structurally blocked from executing in your cluster. More importantly, your security team can sleep soundly, knowing that there is not a single leaked credential in a secrets manager that could compromise your entire deployment infrastructure.