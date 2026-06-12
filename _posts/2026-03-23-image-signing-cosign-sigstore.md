---
layout: post
title: "Image Signing with Cosign and Sigstore in CI/CD Pipelines"
date: 2026-03-23 08:00:00 +0700
tags: [devsecops, kubernetes, containers, supply-chain-security, ci-cd]
description: "How to harden container supply chains using Cosign and Sigstore to ensure only verified, signed images reach your Kubernetes clusters."
image: ""
thumbnail: ""
---

In late 2020, attackers compromised the SolarWinds build pipeline and shipped malicious updates to 18,000 customers. The images were signed — just with a key the attackers had stolen. Three years later, the XZ Utils backdoor sat quietly in build infrastructure for months before a Microsoft engineer noticed. These aren't exotic attacks. They're the predictable result of treating container images as tamper-evident when they're actually tamper-invisible: by default, nothing in your Kubernetes cluster checks whether that `nginx:1.25.3` you pulled was actually built by your CI pipeline, not a compromised registry mirror or a supply chain attacker who pivoted from your dependency tree. Cosign and Sigstore close this gap — not by adding more keys to manage, but by replacing keys with cryptographic identity tied to your CI/CD workload identity. This post walks through production-grade implementation: keyless signing in GitHub Actions, KMS-backed signing for air-gapped environments, verification in admission controllers, and the operational failure modes you need to handle before rolling this to production.

![Image Signing with Cosign and Sigstore in CI/CD Pipelines Diagram](/images/diagrams/image-signing-cosign-sigstore.svg)

## Why Container Image Signing Wasn't Adopted for a Decade

Docker Content Trust (DCT) existed since 2015 using The Update Framework (TUF) and Notary v1. Almost no one runs it in production. The reason is operational: Notary requires you to manage root keys, delegation keys, and a separate Notary server. If you lose your root key, you lose your ability to push to that repository — forever. If your Notary server goes down, pulling becomes degraded. The UX was hostile enough that teams accepted the security gap rather than pay the operational cost.

Sigstore inverts this model. The core insight is that in a CI/CD pipeline, the signer's identity is already established — GitHub Actions has an OIDC token proving it's running `myorg/myrepo` on commit `abc123`. Fulcio (Sigstore's CA) accepts that OIDC token and issues a short-lived certificate (10-minute TTL) binding the signing key to that identity. Cosign signs the image with an ephemeral key, records the signature and certificate in Rekor (Sigstore's transparency log), and throws the private key away. There are no long-lived signing keys to rotate, leak, or audit. The proof of signing lives in Rekor — an append-only, publicly auditable log backed by a Merkle tree.

## Keyless Signing in GitHub Actions

Here's a complete, production-ready GitHub Actions workflow that builds, signs, and attaches an SBOM to your image:

```yaml
# snippet-1
name: build-sign-push

on:
  push:
    branches: [main]
    tags: ["v*"]

permissions:
  contents: read
  packages: write
  id-token: write   # Required for OIDC token — Fulcio needs this

jobs:
  build-and-sign:
    runs-on: ubuntu-latest
    outputs:
      image-digest: ${{ steps.push.outputs.digest }}

    steps:
      - uses: actions/checkout@v4

      - name: Install Cosign
        uses: sigstore/cosign-installer@v3.4.0
        with:
          cosign-release: v2.2.3

      - name: Install Syft (SBOM generator)
        uses: anchore/sbom-action/download-syft@v0.15.9

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        id: push
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: ghcr.io/${{ github.repository }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
          # Build provenance attestation (SLSA Level 2)
          provenance: true
          sbom: true

      - name: Sign image with Cosign (keyless)
        env:
          DIGEST: ${{ steps.push.outputs.digest }}
        run: |
          cosign sign --yes \
            ghcr.io/${{ github.repository }}@${DIGEST}
        # cosign automatically uses OIDC from the GitHub Actions environment
        # Fulcio issues a cert for: https://github.com/myorg/myrepo/.github/workflows/build.yml@refs/heads/main

      - name: Attach SBOM as OCI artifact
        env:
          DIGEST: ${{ steps.push.outputs.digest }}
        run: |
          syft ghcr.io/${{ github.repository }}@${DIGEST} \
            -o spdx-json \
            --file sbom.spdx.json

          cosign attach sbom \
            --sbom sbom.spdx.json \
            --type spdx \
            ghcr.io/${{ github.repository }}@${DIGEST}

          cosign sign --yes \
            --attachment sbom \
            ghcr.io/${{ github.repository }}@${DIGEST}
```

The `id-token: write` permission is the critical piece. Without it, the OIDC token request fails and Fulcio cannot issue a certificate. The signature is stored as an OCI artifact alongside your image — no external storage needed. The format is `sha256-<digest>.sig` in the same repository.

## KMS-Backed Signing for Air-Gapped and Regulated Environments

Keyless signing requires outbound HTTPS to `fulcio.sigstore.dev` and `rekor.sigstore.dev`. For air-gapped environments, financial services with network controls, or organizations that need long-term key accountability, KMS-backed signing is the right model. You manage the key, but Cosign handles the signing protocol:

```bash
# snippet-2
# Create a signing key in AWS KMS (asymmetric, ECDSA P-256)
aws kms create-key \
  --key-spec ECC_NIST_P256 \
  --key-usage SIGN_VERIFY \
  --description "cosign-image-signing-prod" \
  --region us-east-1

# Export the key ID and set the COSIGN_KEY env var
export KMS_KEY_ID="arn:aws:kms:us-east-1:123456789012:key/mrk-abc123"

# Sign image using KMS key
cosign sign \
  --key "awskms:///${KMS_KEY_ID}" \
  --tlog-upload=false \          # Disable Rekor for air-gapped environments
  ghcr.io/myorg/myapp@sha256:abc123...

# Verify with the public key from KMS
cosign verify \
  --key "awskms:///${KMS_KEY_ID}" \
  --insecure-ignore-tlog \       # Must match --tlog-upload=false above
  ghcr.io/myorg/myapp@sha256:abc123...

# For GCP: cosign sign --key gcpkms://projects/myproject/locations/global/keyRings/cosign/cryptoKeyVersions/1
# For Azure: cosign sign --key azurekms://mykeyvault.vault.azure.net/keys/cosign/version
# For HashiCorp Vault: cosign sign --key hashivault://cosign-key
```

One production gotcha: `--tlog-upload=false` must be consistent. If you sign without uploading to Rekor and later try to verify with `--rekor-url`, verification fails because there's no log entry. Bake the verification flags into your admission controller policy, not ad-hoc in scripts.

## Verifying Signatures at Admission: Kyverno Policy

Signing is worthless without enforcement. The signature lives in the registry — if your cluster can pull unsigned images without anyone checking, attackers just push unsigned. Kyverno's `verifyImages` rule calls `cosign verify` inline during the admission review:

```yaml
# snippet-3
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-signed-images
  annotations:
    policies.kyverno.io/title: Require Signed Container Images
    policies.kyverno.io/severity: high
    policies.kyverno.io/category: Supply Chain Security
spec:
  validationFailureAction: Enforce   # Change to Audit first when rolling out
  background: false                  # Only enforce at admission, not background scan
  rules:
    - name: verify-image-signature
      match:
        any:
          - resources:
              kinds: [Pod]
              namespaces:
                - production
                - staging
      verifyImages:
        - imageReferences:
            - "ghcr.io/myorg/*"
          attestors:
            - count: 1
              entries:
                - keyless:
                    subject: "https://github.com/myorg/myrepo/.github/workflows/*.yml@refs/heads/main"
                    issuer: "https://token.actions.githubusercontent.com"
                    rekor:
                      url: https://rekor.sigstore.dev
          # Require SBOM attestation as well
          attestations:
            - predicateType: https://spdx.dev/Document
              attestors:
                - count: 1
                  entries:
                    - keyless:
                        subject: "https://github.com/myorg/myrepo/.github/workflows/*.yml@refs/heads/main"
                        issuer: "https://token.actions.githubusercontent.com"
        # Images from other registries require explicit allowlist
        - imageReferences:
            - "docker.io/library/alpine:*"
            - "docker.io/library/nginx:*"
          skipImageReferences:
            - "docker.io/library/alpine:3.19*"   # Pin to known-good minor
          mutateDigest: true    # Resolve tags to digests at admission — prevents tag mutation attacks
          verifyDigest: true
```

The `subject` field in `keyless` is the critical trust anchor. It pins the acceptable signing identity to a specific GitHub Actions workflow path. An attacker who compromises a different workflow in the same org cannot sign images that pass this check. Use glob matching carefully — `*.yml` is permissive; `build-sign.yml` is tight.

**Start with `Audit` mode.** Switching directly to `Enforce` on an existing cluster will block deployments of any unsigned images currently running, including your monitoring stack and admission controller itself. Run in audit for two weeks, fix everything flagged, then flip to enforce.

## Verifying Signatures in Deployment Scripts

For teams not yet running Kyverno, or for pre-deployment verification in CI before promoting to production, cosign verify is a one-liner:

```bash
# snippet-4
#!/usr/bin/env bash
set -euo pipefail

IMAGE="ghcr.io/myorg/myapp"
DIGEST="${IMAGE_DIGEST:-}"  # Passed from CI as sha256:abc123...

if [[ -z "${DIGEST}" ]]; then
  echo "ERROR: IMAGE_DIGEST must be set. Never verify by tag — tags are mutable."
  exit 1
fi

# Verify signature (keyless)
cosign verify \
  --certificate-identity-regexp="^https://github.com/myorg/myrepo/\.github/workflows/.*@refs/heads/main$" \
  --certificate-oidc-issuer="https://token.actions.githubusercontent.com" \
  "${IMAGE}@${DIGEST}" \
  | jq '.[0] | {subject: .critical.identity.docker-reference, issuer: .optional.Issuer, workflow: .optional.githubWorkflowRef}'

# Verify SBOM attestation exists and is signed
cosign verify-attestation \
  --type spdx \
  --certificate-identity-regexp="^https://github.com/myorg/myrepo/\.github/workflows/.*@refs/heads/main$" \
  --certificate-oidc-issuer="https://token.actions.githubusercontent.com" \
  "${IMAGE}@${DIGEST}" \
  | jq '.payload | @base64d | fromjson | .predicate.name'

echo "✓ Image ${DIGEST} verified — safe to deploy"
```

Always verify by digest, never by tag. Tags are mutable — an attacker with push access to the registry can move a tag to point at an unsigned image after your CI signed and pushed a different one. Lock your deployment manifests to the exact SHA-256 digest your pipeline produces.

## Self-Hosting Rekor and Fulcio

For organizations that cannot send signing events to the public Sigstore infrastructure, the [Scaffold](https://github.com/sigstore/scaffold) project provides Helm charts for self-hosted Rekor and Fulcio. The operational burden is real — Rekor requires a Trillian log server backed by a database (MySQL or CockroachDB), and Fulcio needs a CA backend (Google Certificate Authority Service, AWS Private CA, or CFSSL with a self-signed root).

The minimum viable self-hosted setup for a team of 50 engineers producing ~200 images/day:

- Rekor: 2 replicas × 2 CPU / 4 GB RAM, backed by a 3-node Trillian cluster
- Fulcio: 2 replicas × 1 CPU / 2 GB RAM, backed by your OIDC provider (Keycloak, Okta, or Google Workspace)
- Total storage growth: ~500 MB/year for Rekor's Merkle tree at 200 images/day

Configure Cosign to use your private infrastructure by setting environment variables in CI:

```bash
# snippet-5
# In your CI environment or .env.signing file
export COSIGN_REPOSITORY="your-registry.internal/cosign-signatures"
export SIGSTORE_REKOR_PUBLIC_KEY="$(cat rekor.pub)"
export REKOR_SERVER="https://rekor.internal.example.com"
export FULCIO_SERVER="https://fulcio.internal.example.com"
export COSIGN_MIRROR="https://tuf.internal.example.com"   # TUF mirror for root trust

# Then sign as normal — cosign picks up the env vars
cosign sign --yes ghcr.io/myorg/myapp@sha256:abc123...

# Verify against your private Rekor
cosign verify \
  --rekor-url="https://rekor.internal.example.com" \
  --certificate-identity="..." \
  --certificate-oidc-issuer="https://keycloak.internal.example.com/realms/cicd" \
  ghcr.io/myorg/myapp@sha256:abc123...
```

## Production Failure Modes and Mitigations

**Rekor outage blocks signing.** By default, `cosign sign` fails if it cannot reach Rekor. Add `--tlog-upload=false` as a fallback in CI, paired with `--insecure-ignore-tlog` in verify. Document clearly in your runbook which images were signed without Rekor so you can audit the gap later. Do not silently degrade — alarm on it.

**Clock skew fails Fulcio certificate issuance.** Fulcio's short-lived certificates have a 10-minute TTL. If your CI runner's clock drifts more than 5 minutes, the OIDC token will be expired before the certificate is issued. Enforce NTP sync on self-hosted runners. GitHub-hosted runners are fine.

**Tag mutation attacks survive signature verification.** Cosign signs the content-addressed digest, not the tag. If your Kyverno policy uses `imageReferences: ["myapp:latest"]` but doesn't set `mutateDigest: true`, a pod that was admitted with the signed `latest` can be restarted with a replaced `latest` pointing at a different digest — and the admission webhook won't re-check it. Set `mutateDigest: true` to have Kyverno rewrite the tag to a digest at admission time. After that, kubelet pulls by digest.

**Signature verification adds ~200-400ms to pod admission.** Kyverno calls cosign verify inline, which fetches the signature from the registry and optionally checks Rekor. In clusters with high pod churn (batch jobs, autoscaling), this adds up. Cache signature verification results with `--cache-size=1000` in the Kyverno deployment args, and co-locate Kyverno replicas with your registry endpoints.

**Namespace exemptions are the most common misconfiguration.** Teams exempt `kube-system` from image signing policies because they don't control upstream images (CoreDNS, kube-proxy, metrics-server). That's a reasonable choice. The mistake is also exempting `monitoring`, `logging`, or `ingress-nginx` because it was expedient during rollout. Attackers who compromise those namespaces get network access to everything. Define your exemptions explicitly, review them quarterly, and sign your own infrastructure components.

## Rolling Out to an Existing Cluster

The safe rollout sequence for an existing production cluster:

```yaml
# snippet-6
# Phase 1: Audit mode — no enforcement, just logging
# Deploy Kyverno ClusterPolicy with validationFailureAction: Audit
# Run for 2 weeks, collect violations from kyverno.io/policy-violation events

kubectl get policyviolationreports -A \
  --sort-by='.metadata.creationTimestamp' \
  -o json | jq '.items[] | {ns: .metadata.namespace, policy: .metadata.labels["policy.kubernetes.io/name"], images: [.results[].resources[].name]}'

# Phase 2: Sign all internal images, add signed versions to cluster
# Track unsigned images still running:
kubectl get pods -A -o json \
  | jq '.items[] | select(.metadata.namespace != "kube-system") | {ns: .metadata.namespace, pod: .metadata.name, images: [.spec.containers[].image]}' \
  | grep -v sha256   # Pods without digest pinning are highest risk

# Phase 3: Enforce on non-system namespaces first
# Update ClusterPolicy: validationFailureAction: Enforce
# namespaces: [production, staging]

# Phase 4: Enforce on all namespaces including kube-system
# Ensure ALL infrastructure images are either signed or explicitly allowlisted
```

## What This Does Not Solve

Image signing proves provenance — that an image was built by your CI system at a specific commit. It does not prove the image is safe. A signed image built from a dependency with a known CVE, or containing a backdoored package, is still signed and will pass all admission checks. Signing chains to a security posture only when combined with:

- Continuous vulnerability scanning (Trivy, Grype, or AWS Inspector) on every pushed digest
- SBOM analysis to surface transitive dependency risks
- SLSA provenance attestations to verify the build environment itself wasn't compromised
- Network policies that restrict what signed containers can reach

Cosign and Sigstore solve the distribution integrity problem: you know the image you deployed is the one your CI built. That's a necessary condition for a secure supply chain, not a sufficient one. The rest of the stack — scanning, SBOM, provenance, runtime policies — is what converts that guarantee into actual risk reduction.

The SolarWinds attackers had code signing. They also had full access to the build pipeline. Sigstore's keyless model would have surfaced an anomalous signing identity in the transparency log — a Rekor entry showing a signing event that didn't originate from the expected GitHub Actions workflow. That's auditable. Anomalies become detectable. That's the real value: not preventing the impossible, but making the attack surface observable.
