---
layout: post
title: "Sigstore Keyless Signing in CI/CD Pipelines"
date: 2026-03-26 08:00:00 +0700
tags: [devsecops, ci-cd, supply-chain-security, containers, sigstore]
description: "How to eliminate long-lived signing keys from your CI/CD pipeline using Sigstore's keyless signing workflow with OIDC-backed ephemeral credentials."
image: ""
thumbnail: ""
---

Last year, a compromised npm package with a valid PGP signature made it to thousands of production systems before anyone noticed. The signature was legitimate — the private key had leaked months earlier, and nobody knew. That's the fundamental problem with traditional artifact signing: you're betting your entire supply chain on the secrecy of a long-lived private key stored somewhere in a secrets manager, rotated infrequently, and accessible to anyone with the right IAM role. One leaked key, one compromised CI runner, one misconfigured secret — and every signed artifact you've ever produced is now suspect.

Sigstore's keyless signing flips this model. Instead of a persistent private key, you get ephemeral credentials tied to a short-lived OIDC identity — your GitHub Actions workflow identity, your GCP service account, your GitLab CI job token. The private key exists for seconds, signs the artifact, and disappears. What remains is a verifiable audit trail in Rekor, Sigstore's public transparency log. There's no key to steal, no rotation schedule to miss, and no "but how do we know the key wasn't compromised six months ago?"

This post covers integrating Sigstore's keyless signing into a real CI/CD pipeline — GitHub Actions with container images and Go binaries — including the verification side, policy enforcement, and the sharp edges you'll hit in production.

## How Keyless Signing Actually Works

The mechanism relies on three components: Fulcio (certificate authority), Rekor (transparency log), and your existing OIDC provider.

The flow during signing:
1. Your CI job requests an OIDC token from the provider (GitHub, GitLab, GCP, etc.)
2. Cosign sends that token to Fulcio, which issues a short-lived X.509 certificate binding your OIDC identity to a freshly generated ephemeral key pair
3. Cosign signs the artifact with the ephemeral private key
4. The signature and certificate are published to Rekor
5. The ephemeral private key is discarded

Fulcio's certificate has a 10-minute TTL. The signature in Rekor is permanent and timestamped. When you verify later, you check that the signature was made during the certificate's validity window and that the certificate identity matches what you expect. The ephemeral key being gone is irrelevant — the Rekor entry proves the signature happened when the certificate was valid.

This is a fundamentally different trust model: instead of trusting that a key is still secret, you're trusting that the OIDC identity issuer (GitHub, GCP, etc.) correctly attested the workload identity at signing time.

## Signing Container Images in GitHub Actions

Here's a production workflow signing a multi-arch container image. The critical detail is the `id-token: write` permission — without it, GitHub won't issue the OIDC token that Fulcio needs.

```yaml
# // snippet-1
name: build-and-sign

on:
  push:
    tags: ['v*']

permissions:
  contents: read
  packages: write
  id-token: write  # Required for OIDC token issuance

jobs:
  build-sign-push:
    runs-on: ubuntu-latest
    env:
      REGISTRY: ghcr.io
      IMAGE_NAME: ${{ github.repository }}

    steps:
      - uses: actions/checkout@v4

      - name: Install Cosign
        uses: sigstore/cosign-installer@v3
        with:
          cosign-release: 'v2.4.0'

      - name: Log in to registry
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Build and push
        id: build-push
        uses: docker/build-push-action@v6
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:${{ github.ref_name }}
          # Critical: capture the digest, not just the tag
          outputs: type=image,name=${{ env.REGISTRY }}/${{ env.IMAGE_NAME }},push-by-digest=true,name-canonical=true,push=true

      - name: Sign image by digest
        env:
          DIGEST: ${{ steps.build-push.outputs.digest }}
        run: |
          cosign sign --yes \
            "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}@${DIGEST}"
```

Sign by digest, not tag. Tags are mutable. If you sign `myimage:v1.2.3` and someone pushes a new image with that tag, your signature is now attached to a different artifact. Signing by digest (`sha256:abc123...`) is immutable and unambiguous.

## Signing Go Binaries and Generating SBOMs

Container images aren't the only artifact worth signing. If you're distributing Go binaries — CLI tools, sidecar agents, anything users download and run — sign those too. Pair signing with an SBOM for a complete supply chain story.

```yaml
# // snippet-2
      - name: Build release binaries
        run: |
          GOOS=linux GOARCH=amd64 go build -ldflags="-s -w -X main.version=${{ github.ref_name }}" \
            -o dist/myapp-linux-amd64 ./cmd/myapp
          GOOS=darwin GOARCH=arm64 go build -ldflags="-s -w -X main.version=${{ github.ref_name }}" \
            -o dist/myapp-darwin-arm64 ./cmd/myapp

      - name: Generate SBOM with syft
        uses: anchore/sbom-action@v0
        with:
          path: ./dist
          format: spdx-json
          output-file: dist/sbom.spdx.json

      - name: Sign binaries and SBOM
        run: |
          for artifact in dist/myapp-linux-amd64 dist/myapp-darwin-arm64 dist/sbom.spdx.json; do
            cosign sign-blob --yes \
              --bundle="${artifact}.bundle" \
              "${artifact}"
          done

      - name: Upload release artifacts
        uses: softprops/action-gh-release@v2
        with:
          files: |
            dist/myapp-linux-amd64
            dist/myapp-linux-amd64.bundle
            dist/myapp-darwin-arm64
            dist/myapp-darwin-arm64.bundle
            dist/sbom.spdx.json
            dist/sbom.spdx.json.bundle
```

The `.bundle` file is a JSON containing the signature, certificate, and Rekor transparency log entry. Ship it alongside the binary. Users verify the binary, the certificate identity, and optionally check Rekor directly.

## Verifying Signatures: The Part Most Teams Skip

Signing without enforcing verification in production is security theater. Here's verification for both the container image and blob cases:

```bash
# // snippet-3

# Verify a container image — enforce the exact GitHub Actions workflow identity
cosign verify \
  --certificate-identity-regexp="https://github.com/myorg/myrepo/.github/workflows/build-and-sign.yml@refs/tags/v.*" \
  --certificate-oidc-issuer="https://token.actions.githubusercontent.com" \
  ghcr.io/myorg/myrepo@sha256:abc123def456...

# Verify a blob with its bundle
cosign verify-blob \
  --bundle=myapp-linux-amd64.bundle \
  --certificate-identity-regexp="https://github.com/myorg/myrepo/.github/workflows/build-and-sign.yml@refs/tags/v.*" \
  --certificate-oidc-issuer="https://token.actions.githubusercontent.com" \
  myapp-linux-amd64

# Inspect what's in a bundle before trusting it
cat myapp-linux-amd64.bundle | jq '{
  mediaType: .mediaType,
  issuer: .verificationMaterial.certificate.rawBytes | @base64d | ltrimstr("\u0000") | . as $pem | "check with openssl",
  rekorLogIndex: .verificationMaterial.tlogEntries[0].logIndex
}'
```

The `--certificate-identity-regexp` flag is where your security actually lives. Too broad a pattern (like just matching on `myorg/myrepo` without the specific workflow path) lets any workflow in your repo sign as if it were your release pipeline. Lock it down to the specific workflow file and ref pattern that should be producing release artifacts.

## Policy Enforcement with Kyverno

Verifying manually is fine for humans downloading CLI tools. For Kubernetes workloads, you need admission control. Kyverno's ClusterPolicy with Sigstore support enforces signing at deploy time:

```yaml
# // snippet-4
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-signed-images
  annotations:
    policies.kyverno.io/title: Require Signed Images
    policies.kyverno.io/description: >
      All container images must be signed via Sigstore keyless signing
      from the organization's GitHub Actions release workflow.
spec:
  validationFailureAction: Enforce
  background: false
  rules:
    - name: check-image-signature
      match:
        any:
          - resources:
              kinds: [Pod]
              namespaces: [production, staging]
      verifyImages:
        - imageReferences:
            - "ghcr.io/myorg/*"
          attestors:
            - count: 1
              entries:
                - keyless:
                    subject: "https://github.com/myorg/*/. github/workflows/build-and-sign.yml@refs/tags/v*"
                    issuer: "https://token.actions.githubusercontent.com"
                    rekor:
                      url: https://rekor.sigstore.dev
          mutateDigest: true   # Rewrites image:tag to image@digest on admission
          verifyDigest: true
          required: true
```

`mutateDigest: true` is worth calling out explicitly. When this policy admits a pod, it rewrites the image reference from tag to digest in the pod spec. This means even if someone pushes a new image to that tag later, running pods aren't affected, and you have an immutable record of exactly what image was deployed. Pair this with your audit logs and you have a complete chain of custody.

## The Rekor Transparency Log in Practice

Every keyless signature creates a Rekor entry. You can query it directly:

```bash
# // snippet-5

# Look up entries for a specific image digest
rekor-cli search \
  --sha "sha256:abc123def456789..." \
  --rekor_server https://rekor.sigstore.dev

# Get full entry details by log index
rekor-cli get \
  --log-index 12345678 \
  --rekor_server https://rekor.sigstore.dev \
  --format json | jq '{
    logID: .logID,
    logIndex: .logIndex,
    integratedTime: .integratedTime | todate,
    body: .body | @base64d | fromjson | {
      kind: .kind,
      spec: .spec.signature.content | @base64d | . as $sig | {
        certificateSubject: "extracted from cert",
        artifactHash: .spec.data.hash
      }
    }
  }'

# Verify an inclusion proof (Rekor's cryptographic guarantee the entry is in the log)
rekor-cli verify \
  --artifact myapp-linux-amd64 \
  --signature myapp-linux-amd64.sig \
  --rekor_server https://rekor.sigstore.dev
```

The Rekor public instance is operated by Sigstore (now part of the Linux Foundation). Entries are immutable and append-only, backed by a Merkle tree with a signed tree head that you can independently verify. In regulated environments, you may need to run your own Rekor instance — the [sigstore/rekor](https://github.com/sigstore/rekor) repo is the reference implementation, and it runs fine in a Kubernetes cluster behind a load balancer.

## Sharp Edges You'll Hit

**Clock skew kills verification.** Fulcio certificates have a 10-minute TTL. If your CI runner's clock is drifting, Cosign will refuse to sign because the certificate may already be expired by the time the signing request arrives. Enforce NTP synchronization on your runners. This sounds obvious until your self-hosted runners have 45-second clock drift because someone forgot to configure `chronyd`.

**Rekor rate limits exist.** The public Rekor instance (`rekor.sigstore.dev`) has rate limits. At high build volume — say, 500+ builds per day — you'll start seeing intermittent failures. The limit as of 2025 was around 1,000 requests per minute from a single IP. If you're running builds on a small pool of fixed-IP runners, you can hit this. Either distribute your runners across more IPs or run a private Rekor instance.

**Signature verification adds latency to pod admission.** Kyverno has to fetch the image manifest and verify the signature on every pod admission. In clusters with rapid autoscaling, this can add 2-5 seconds to pod startup. Profile your admission webhook latency before enforcing this policy on latency-sensitive workloads.

**Not all registries support OCI artifact attachment equally.** Cosign attaches signatures as OCI artifacts to the same registry. ECR's support for OCI artifacts was incomplete for a long time. As of late 2024, ECR supports it, but with caveats around cross-account access. If you're using ECR with cross-account image pulls, test signature verification explicitly in your non-prod accounts before going to prod.

**The `--yes` flag in CI.** Without `--yes`, Cosign prompts interactively for confirmation in keyless mode. Scripts and CI pipelines fail silently or hang. Always include `--yes` in automated contexts.

## What This Actually Buys You in Production

After integrating keyless signing, what changes concretely?

You eliminate the "rotate all the signing keys" incident response procedure. When a key leaks — and eventually one will — you're rekeying certificates that expire in 10 minutes rather than auditing two years of artifacts signed with a potentially compromised key.

You get a tamper-evident audit trail for free. Every signature in Rekor is timestamped and tied to a specific workload identity. When security asks "was this image built from a tagged release or from a branch commit," you can answer in 30 seconds by querying Rekor, not by hoping someone wrote something in a ticket.

You make supply chain attestation a first-class part of your pipeline rather than a compliance checkbox. The SBOM is signed. The binary is signed. The container is signed. The signatures are verifiable by anyone with the public Rekor log.

The tooling is mature enough for production now. Cosign has been at v2.x since 2023. The keyless infrastructure is running at scale — as of early 2026, Rekor has over 1.5 billion entries. The sharp edges are real but they're documented and workable. For any team shipping software to production in 2026, eliminating long-lived signing keys should be on the roadmap. The cost is one afternoon of CI pipeline work. The alternative is explaining to your customers why a leaked signing key from 18 months ago means they need to treat every binary you've ever shipped as suspect.
```