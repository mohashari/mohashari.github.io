---
layout: post
title: "Hardening Software Supply Chain: Automating Cosign Keyless Container Signing and Verification in Tekton CI/CD"
date: 2026-07-20 08:00:00 +0700
tags: [devsecops, kubernetes, security, tekton, sigstore]
description: "Eliminate static private keys from your CI/CD. Learn to automate Sigstore Cosign keyless signing in Tekton pipelines and enforce Kyverno validation."
image: "https://picsum.photos/seed/9651/1080/720"
thumbnail: "https://picsum.photos/seed/9651/400/300"
---

Consider the following scenario: an attacker gains write access to your container registry through a leaked developer credential. They push a backdoored version of a critical production image using an existing tag. Without cryptographic validation at the Kubernetes admission control gate, your cluster will happily pull and run the compromised payload on the next deployment or replica scale-up. Standard cryptographic signing traditionally solved this, but it introduced a notorious key management nightmare: engineers checking private signing keys into Git, certificates expiring silently, and the overhead of secure key rotation. Keyless signing using Sigstore (Cosign, Fulcio, and Rekor) eliminates static keys entirely by linking container identity to an ephemeral OpenID Connect (OIDC) identity. By integrating keyless signing directly into your Tekton CI/CD pipelines, you ensure that only container images built by authorized workloads can ever run in production.

## The Ephemeral Trust Loop: Fulcio, Rekor, and OIDC

Keyless signing is a misnomer; cryptographic keys are still used, but their lifecycle is reduced to minutes, and they are never stored on disk. Instead of managing a long-lived private key, the Tekton pipeline run uses its short-lived Kubernetes Service Account OIDC token as proof of identity. The process follows a strict cryptographic handshake:

1. **Local Key Generation:** The Cosign task in the Tekton pipeline generates a temporary, single-use public/private key pair in memory.
2. **OIDC Authentication:** The task fetches the projected OIDC token from the Kubernetes Pod environment.
3. **Fulcio Certificate Issuance:** Cosign sends the OIDC token and the temporary public key to Fulcio (the Sigstore Certificate Authority). Fulcio validates the token's signature, extracts the identity (e.g., the Tekton Service Account name and namespace), and issues a short-lived X.509 certificate (valid for 10 minutes) containing this identity.
4. **Digest Signing:** Cosign signs the container image's immutable cryptographic digest (not the mutable tag) using the temporary private key.
5. **Transparency Logging:** The signature, public certificate, and artifact metadata are uploaded to Rekor (the Sigstore cryptographically verifiable transparency log).
6. **Key Destruction:** The private key is immediately purged from memory.

To verify the container image at deploy time, Kubernetes admission controllers do not need a public key. They query the Rekor log, verify the Fulcio certificate chain, and match the OIDC claims in the certificate against a set of declared policy rules. If an attacker tries to sign an image with their own OIDC identity, the admission controller blocks the deployment because the identity does not match the approved pipeline.

## Configuring Tekton for OIDC Token Projection

To facilitate keyless signing, the Tekton Task running Cosign must have access to a projected OIDC token issued by the Kubernetes API server. This token must specify the target audience (typically `sigstore`). In modern Kubernetes clusters, the default service account token projected to `/var/run/secrets/kubernetes.io/serviceaccount` is scoped for the API server and may not be accepted by Fulcio.

We must define a dedicated ServiceAccount in the Tekton build namespace and configure the Pod running the task to project a secondary volume containing a OIDC token with the correct audience.

## Step 1: Building and Capturing Digests with Kaniko

A primary rule of container security is to never sign images by their mutable tag (e.g., `:latest` or `:v1.2.0`). Tags are pointers that can be reassigned. You must sign the immutable image digest (`sha256:...`). 

The first step in our pipeline is a Kaniko build task. Kaniko is ideal for Kubernetes-native environments because it executes user-space container builds without requiring a privileged Docker daemon. The critical requirement for this task is to capture the image digest generated during the push phase and write it to a Tekton Task Result, making it available to subsequent pipeline stages.

```yaml
// snippet-1
apiVersion: tekton.dev/v1
kind: Task
metadata:
  name: kaniko-build
  namespace: cicd-pipelines
spec:
  params:
    - name: IMAGE
      type: string
      description: Name of the image to build (without tag/digest)
    - name: CONTEXT
      type: string
      default: ./
      description: Path to the build context
  results:
    - name: IMAGE_DIGEST
      description: The digest of the pushed container image
  steps:
    - name: build-and-push
      image: gcr.io/kaniko-project/executor:v1.20.0
      securityContext:
        runAsUser: 0
      command:
        - /kaniko/executor
      args:
        - --context=$(params.CONTEXT)
        - --dockerfile=Dockerfile
        - --destination=$(params.IMAGE)
        - --digest-file=$(results.IMAGE_DIGEST.path)
```

## Step 2: Designing the Cosign Keyless Signing Task

Once the image is pushed and its digest is outputted, the Cosign task starts. The following task mounts a projected volume containing the OIDC identity token and exposes it to Cosign via the `SIGSTORE_ID_TOKEN` environment variable. By pointing Cosign directly to this token, we avoid any interactive OAuth login prompts, making the process fully autonomous.

```yaml
// snippet-2
apiVersion: tekton.dev/v1
kind: Task
metadata:
  name: cosign-sign
  namespace: cicd-pipelines
spec:
  params:
    - name: IMAGE_DIGEST
      type: string
      description: The fully qualified image digest (registry/repo@sha256:hash)
  steps:
    - name: sign-image
      image: gcr.io/projectsigstore/cosign:v2.2.3
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        runAsNonRoot: true
        runAsUser: 1000
      volumeMounts:
        - name: oidc-token
          mountPath: /var/run/secrets/sigstore-token
          readOnly: true
      env:
        - name: SIGSTORE_ID_TOKEN
          value: /var/run/secrets/sigstore-token/token
        - name: COSIGN_EXPERIMENTAL
          value: "1"
      command:
        - cosign
      args:
        - sign
        - --yes
        - $(params.IMAGE_DIGEST)
  volumes:
    - name: oidc-token
      projected:
        sources:
          - serviceAccountToken:
              path: token
              audience: sigstore
              expirationSeconds: 600
```

## Step 3: Orchestrating the Tekton Pipeline

We assemble these tasks into a single Tekton Pipeline. The pipeline coordinates the dependencies, ensuring that the `cosign-sign` task executes if and only if the `kaniko-build` task completes successfully. We pass the output of the builder task directly to the input of the signer task using Tekton's results syntax: `$(tasks.kaniko-build.results.IMAGE_DIGEST)`.

```yaml
// snippet-3
apiVersion: tekton.dev/v1
kind: Pipeline
metadata:
  name: secure-build-pipeline
  namespace: cicd-pipelines
spec:
  params:
    - name: IMAGE_NAME
      type: string
      description: Target registry destination for the container image
  tasks:
    - name: build-image
      taskRef:
        name: kaniko-build
      params:
        - name: IMAGE
          value: $(params.IMAGE_NAME)
        - name: CONTEXT
          value: "/workspace/source"
    - name: sign-image
      taskRef:
        name: cosign-sign
      runAfter:
        - build-image
      params:
        - name: IMAGE_DIGEST
          value: $(params.IMAGE_NAME)@$(tasks.build-image.results.IMAGE_DIGEST)
```

## Step 4: Cluster Admission Control with Kyverno

Signing container images does not improve security if the target Kubernetes cluster does not enforce verification. Kyverno is a Kubernetes-native policy engine that integrates seamlessly with Sigstore.

Simply checking if an image has a signature is insufficient; attackers can easily sign their own malicious images using their own credentials on the public Sigstore infrastructure. The Kyverno policy must explicitly assert that the signature was generated by the specific OIDC identity (Service Account name) in the specific namespace where our Tekton CI pipeline runs, issued by the authorized OIDC provider.

```yaml
// snippet-4
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: enforce-keyless-signing
spec:
  validationFailureAction: Enforce
  background: false
  rules:
    - name: verify-tekton-signature
      match:
        any:
          - resources:
              kinds:
                - Pod
      exclude:
        resources:
          namespaces:
            - kube-system
            - cicd-pipelines
      mutate:
        foreach:
          - list: "request.object.spec.containers"
            patchStrategicMerge:
              spec:
                containers:
                  - name: "{{ element.name }}"
                    image: "{{ element.image }}"
      validate:
        imageVerification:
          - imageReferences:
              - "registry.internal.corp/production/*"
            attestations: []
            verifyDigest: true
            required: true
            keyless:
              authority:
                issuer: "https://kubernetes.default.svc.cluster.local"
                subject: "system:serviceaccount:cicd-pipelines:pipeline-runner"
```

## Step 5: Manual Verification and Log Auditing

Security and operations teams must be able to audit and verify these images manually outside of the automated Kubernetes admission control context. You can use the `cosign verify` command-line utility to query Fulcio and Rekor manually. 

```bash
// snippet-5
# Export the target image containing the exact digest to verify
export TARGET_IMAGE="registry.internal.corp/production/payment-gateway@sha256:7f015b6d5..."

# Perform out-of-band validation against the Kubernetes OIDC issuer constraints
cosign verify \
  --certificate-identity-regexp "^system:serviceaccount:cicd-pipelines:pipeline-runner$" \
  --certificate-oidc-issuer "https://kubernetes.default.svc.cluster.local" \
  "$TARGET_IMAGE"

# Query the Rekor transparency log using the image digest to assert inclusion
cosign search "$TARGET_IMAGE"
```

## Production Failure Modes and Mitigations

Deploying OIDC keyless signing in a high-throughput enterprise environment exposes several complex failure modes that must be proactively mitigated before transitioning workload enforcement to `Enforce` mode.

### 1. Clock Skew and the Ephemeral Certificate Lifetime
Because Fulcio issues certificates with a lifetime of only 10 minutes, time synchronization across your CI/CD runner nodes and Kubernetes control plane is critical. 
* **The Failure:** If the host clock on a Tekton runner drifts by as little as 5 minutes from the Fulcio server time, the generated OIDC token may be rejected, or the resulting certificate will be generated with a validity window that has already expired or is about to expire before the Rekor upload completes.
* **The Mitigation:** Configure aggressive NTP synchronization checks on all Kubernetes worker nodes. Implement node-level monitoring to alert if `chronyd` drifts by more than 1 second from upstream reference servers.

### 2. Rekor API Rate Limiting and Performance Bottlenecks
Public Sigstore infrastructure (`rekor.sigstore.dev` and `fulcio.sigstore.dev`) is shared globally. High-volume CI pipelines running dozens of parallel jobs will frequently trigger rate-limiting blocks (HTTP 429).
* **The Failure:** A build task fails midway because Cosign cannot submit the signature artifact to Rekor. Conversely, at deploy time, Kyverno may timeout trying to pull validation records, blocking cluster deployments.
* **The Mitigation:** For production workloads, deploy a private Sigstore instance inside your network using the official Sigstore Helm charts. Configure Cosign and Kyverno to reference your private endpoints by setting the environment variables `TUF_ROOT` and pointing `cosign` to your local targets.

### 3. Admission Control Webhook Timeout Failures
When Kyverno intercepts a Pod creation request, it must resolve the container image tag to a digest (if not already defined) and reach out to the registry and the Rekor server to verify the signatures.
* **The Failure:** If the container registry or Rekor server experiences transient latency, the mutation/validation webhook will timeout. If the Webhook's `failurePolicy` is set to `Fail`, the entire deployment will block, causing cascading failures in GitOps synchronizations (e.g., ArgoCD showing synchronization loops).
* **The Mitigation:** Never use `:latest` tags in GitOps. Ensure all deployment manifests specify the immutable image digest (`image: repo@sha256:...`). This prevents Kyverno from making synchronous API calls to the registry to resolve tags. Additionally, tune the Kyverno `webhookConfiguration` timeouts, setting them to a maximum of 15 seconds to handle transient network blips gracefully.