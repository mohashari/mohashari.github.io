---
layout: post
title: "Preventing Supply Chain Attacks by Enforcing Cryptographic Software Bill of Materials (SBOM) Attestation in Kubernetes Admission Controllers"
date: 2026-08-04 08:00:00 +0700
tags: [kubernetes, devsecops, sbom, container-security]
description: "Secure your Kubernetes supply chain by enforcing signed SBOM attestations at the admission controller level using Kyverno and Cosign."
image: "https://picsum.photos/seed/1415/1080/720"
thumbnail: "https://picsum.photos/seed/1415/400/300"
---

In modern containerized production environments, checking image signatures alone is a security theater that fails to address the root vector of supply chain attacks. When malicious actors compromise CI/CD runners (as seen in the SolarWinds and Codecov breaches) or poison upstream dependencies (such as npm or PyPI packages), they can inject malicious payloads directly into the build artifact *before* it is signed. To the downstream Kubernetes cluster, the final container image appears completely legitimate because it carries a valid signature from your CI/CD private key. To stop these attacks, you must enforce a Zero-Trust image entry policy that parses and validates a cryptographically signed Software Bill of Materials (SBOM) before permitting a container to execute. By implementing signed SBOM attestations and verifying them at the Kubernetes admission control gate, you ensure that every dependency, base image layer, and compiled binary is audited, matched against compliance policies, and verified for integrity before a single CPU cycle is scheduled.

![Preventing Supply Chain Attacks by Enforcing Cryptographic Software Bill of Materials (SBOM) Attestation in Kubernetes Admission Controllers Diagram](/images/diagrams/preventing-supply-chain-attacks-cryptographic-sbom-attestation-kubernetes-admission-controllers.svg)

## The Supply Chain Blind Spot: Why Image Signatures Are Not Enough

Standard container image signing (using tools like Cosign or Notary) verifies the identity of the publisher and the integrity of the image digest. It guarantees that "Image X was built by Pipeline Y and has not changed since." What it does *not* guarantee is the safety of the contents within that image. If your developers pull a compromised base image, or if a build-time dependency runs a malicious post-install script, the container image will still compile successfully, get pushed to your registry, and receive a cryptographically valid signature. 

Once this image is deployed, your Kubernetes cluster runs it blindly. The traditional security posture relies on scanning images *after* they are pushed or periodically scanning running pods. This is reactive. By the time a vulnerability scanner identifies a critical CVE or a malicious dependency in a running container, the attacker may have already executed code, exfiltrated credentials from the pod's service account, or moved laterally within your private VPC.

An SBOM is a comprehensive, machine-readable inventory of all software components, libraries, and modules contained within an image. However, an unsigned SBOM is useless in production; an attacker who can modify a container image can easily regenerate or alter a plain JSON SBOM to hide malicious packages. Cryptographic attestation solves this. An attestation is a signed statement (following the in-toto specification) that binds the SBOM directly to the container image's SHA256 digest. 

By enforcing this attestation in a Kubernetes Admission Webhook, we block the deployment of any container that lacks a valid, signed SBOM matching the running digest. This effectively establishes a continuous audit loop: if it wasn't explicitly inventory-tracked and signed by the trusted build system, it cannot run.

## The Anatomy of an SBOM Attestation

To establish a cryptographically verifiable supply chain, we need to generate an SBOM, wrap it in a signed envelope, and push it to our OCI registry alongside the image. We use two main tools:
1. **Syft** (by Anchore) to inspect the container filesystem and generate a CycloneDX or SPDX-formatted SBOM.
2. **Cosign** (by Sigstore) to sign the SBOM and upload it to the registry as an in-toto attestation.

The attestation is formatted as a DSSE (Dead Simple Signing Envelope) containing a JSON payload. This payload consists of:
*   **Subject**: The image reference and its precise SHA256 digest.
*   **Predicate Type**: A URI specifying the schema of the metadata (for SPDX JSON, this is `https://spdx.dev/Document`).
*   **Predicate**: The actual SBOM content.

When Cosign uploads this attestation, it pushes it to the OCI registry as a separate tag associated with the image's digest. The tag is deterministically named using the format `sha256-<digest>.att`.

Below is a production-quality script showing how to build a container image, generate a clean SPDX SBOM using Syft, and sign it as an attestation using Cosign.

<script src="https://gist.github.com/mohashari/7a8348aa3edff847a22c764a7572fb97.js?file=snippet-1.sh"></script>

## Enforcing SBOMs at the Gate: Kyverno vs. Policy Controller

Once your pipeline generates and pushes attestations, the next step is enforcement. If the Kubernetes API server does not validate the existence of the signed SBOM during pod creation, the security chain is broken. Two primary tools solve this at the admission controller layer: Sigstore's native **Policy Controller** and **Kyverno**.

While Policy Controller is specialized and lightweight, **Kyverno** is the preferred choice for enterprise production environments. Kyverno integrates deeply with the standard Kubernetes resource model, requiring no learning of external DSLs (like Rego). Kyverno allows us to write native YAML policies that intercept `Pod` creation, extract image details, retrieve signed attestations from the registry, verify the signature against public keys (or Sigstore's Fulcio/Rekor for keyless mode), and inspect the inner payload.

The Kyverno `ClusterPolicy` in the snippet below intercepts all pod creations in non-exempt namespaces, checks if the image matches our corporate registry pattern, and verifies that a valid SBOM attestation exists and is signed by our trusted public key.

<script src="https://gist.github.com/mohashari/7a8348aa3edff847a22c764a7572fb97.js?file=snippet-2.yaml"></script>

## Deep Dive: Validating Vulnerabilities and Licenses Inside the Cluster

Verifying that an SBOM *exists* prevents raw, unvetted code from running, but the true power of admission-level DevSecOps lies in examining the *content* of that SBOM before permitting deployment. Kyverno allows you to inspect the decrypted and decoded JSON payload of the in-toto attestation.

For example, your security policy might mandate:
1.  **Dependency Exclusion**: Block any image that contains known high-risk or banned libraries (e.g., outdated versions of `log4j-core`).
2.  **License Compliance**: Block any container containing dependencies licensed under restrictive terms like GPL-3.0 if your application is proprietary.
3.  **Vulnerability Thresholds**: Enforce that a signed vulnerability scan attestation (from Trivy or Grype) accompanies the pod and confirms zero `CRITICAL` vulnerability counts.

The following policy demonstrates content validation. The first rule parses the SBOM attestation's package list to block outdated dependencies, and the second rule validates a signed Trivy vulnerability scan attestation to ensure no critical vulnerabilities exist.

<script src="https://gist.github.com/mohashari/7a8348aa3edff847a22c764a7572fb97.js?file=snippet-3.yaml"></script>

<script src="https://gist.github.com/mohashari/7a8348aa3edff847a22c764a7572fb97.js?file=snippet-4.yaml"></script>

## Hands-On: Building the End-to-End Pipeline

To scale this across production teams, manual script signing is impractical. You must integrate SBOM generation and signing directly into your CI/CD runner environments. 

The most secure approach uses **Keyless signing** via Sigstore (Fulcio and Rekor). Rather than managing static, long-lived private keys inside CI/CD secrets (which are highly vulnerable to leakage), Keyless signing leverages short-lived OpenID Connect (OIDC) identities. 

During a run, the GitHub Actions worker requests an OIDC identity token from GitHub. This token is passed to Fulcio (the Sigstore Certificate Authority), which validates the identity and issues a short-lived x509 certificate valid for only 10 minutes. The signature is then published to the public (or private) Rekor transparency ledger. Kubernetes verifies this by validating the cryptographic signature and matching the certificate's identity claims (e.g., checking that the issuer is GitHub Actions and the repository matches your organization).

Here is a production-grade GitHub Actions workflow that executes this pipeline.

<script src="https://gist.github.com/mohashari/7a8348aa3edff847a22c764a7572fb97.js?file=snippet-5.yaml"></script>

## Handling the Edge Cases: Break-Glass Procedures and Performance

Deploying admission controllers that block production deployments requires careful mitigation of real-world failure modes.

### 1. Registry Latency and Outages
When a node scales out, or a pod restarts, the Kubelet calls the API server, which triggers the Kyverno mutating/validating webhook. Kyverno must resolve the image digest and pull the attestation layer from the OCI registry to verify the signature. 
*   **The Risk**: If your container registry experiences an outage, or if network latency spikes, the admission controller timeout might trigger.
*   **The Mitigation**: 
    1. Configure webhook timeouts defensively (e.g., set `failurePolicy: Ignore` ONLY if you have an auditing engine catching anomalies later, or keep `failurePolicy: Fail` with a generous timeout of 10-15 seconds).
    2. Leverage Kyverno's internal image verification cache to prevent querying the registry for every single duplicate container scale-up event.
    3. Run local caching OCI proxies (like Harbor or registry mirrors) near your clusters.

### 2. Break-Glass / Emergency Bypass
In a severe outage or a zero-day event, you may need to bypass validation policies immediately to deploy an emergency patch before the CI/CD pipeline completes its long testing and attestation steps.
*   **The Bad Way**: Disabling the Kyverno controller or marking the entire namespace as exempt. This opens a window of vulnerability where any unverified image can slip in.
*   **The Secure Way**: Implementing a multi-key "Break-Glass" model. 
Instead of disabling security checks, allow deployments signed by an offline "emergency" private key. The public key is embedded in the Kyverno policy alongside the standard CI key. To deploy an emergency image, a designated security lead signs the image manually using this key, which automatically triggers an audit trail (e.g., via SIEM monitoring for that specific public key thumbprint).

The policy below shows how to allow either the standard pipeline attestation or an emergency signature to authorize a pod deployment.

<script src="https://gist.github.com/mohashari/7a8348aa3edff847a22c764a7572fb97.js?file=snippet-6.yaml"></script>

## Conclusion: Building a Zero-Trust Container Lifecycle

Shifting security left must not stop at static code analysis. Cryptographically binding a signed Software Bill of Materials (SBOM) to your images and actively validating this policy inside Kubernetes is the most robust way to protect your production workloads from supply chain exploits. 

By utilizing **Syft**, **Cosign Keyless signing**, and **Kyverno**, you eliminate raw trust assumptions. Instead of assuming an image is safe because it resides in your private container registry, your Kubernetes cluster verifies the image's components and compliance status dynamically on every deployment. This ensures that even if build infrastructure or upstream code repositories are compromised, your runtime environment remains locked down.