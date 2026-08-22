---
layout: post
title: "Hardening OIDC Federation: Securing Cross-Cloud AWS Role Assumption from GitHub Actions Pipelines"
date: 2026-08-22 08:00:00 +0700
tags: [aws, github-actions, oidc, devsecops, security]
description: "Eliminate static AWS keys. Harden OIDC federation with GitHub Actions using environment-scoped trust policies, ABAC role chaining, and Athena auditing."
image: "https://picsum.photos/seed/1688/1080/720"
thumbnail: "https://picsum.photos/seed/1688/400/300"
---

Static credentials are the asbestos of modern cloud engineering: cheap to deploy, highly toxic, and incredibly difficult to fully remediate once they have spread across a legacy codebase. For years, the default method for authenticating GitHub Actions pipelines to AWS was to generate an IAM User access key pair, paste it into GitHub Repository Secrets, and hope no one accidentally printed the environment variables in a debugging run or leaked them via a compromised dependency. In a large engineering organization running hundreds of microservices, static keys inevitably leak. OIDC (OpenID Connect) federation solves this by replacing permanent keys with short-lived Security Token Service (STS) temporary credentials. However, a naive OIDC integration introduces a silent, catastrophic lateral-movement vulnerability. If your AWS IAM trust policies are not hardened with surgical precision, a single compromised pipeline in a staging repository, a dependency confusion attack in a pull request, or even a scratch repository created by a developer under your GitHub organization can assume your production deployment roles. This post details how to lock down GitHub Actions to AWS OIDC federation, implement environment-based isolation, establish secure cross-account role chaining, and audit the federation flow in production.

## The Core Mechanics of OIDC Federation Between GitHub and AWS

To secure OIDC federation, you must first understand the cryptographic handshake. Rather than storing a long-lived secret, the GitHub Actions runner requests an ephemeral JSON Web Token (JWT) from GitHub's OIDC provider. The runner then presents this JWT to the AWS Security Token Service (STS) using the `AssumeRoleWithWebIdentity` API call. AWS STS verifies the token signature against GitHub's public keys and returns temporary AWS credentials.

The integrity of this handshake relies on the claims embedded within the GitHub-issued JWT. When the runner requests a token, the GitHub OIDC provider signs a payload containing metadata about the workflow run.

Here is what a decrypted OIDC JWT from a GitHub Actions run looks like:

<script src="https://gist.github.com/mohashari/54fac57f10040c9b2cf7e85b7f541665.js?file=snippet-1.json"></script>

Every claim in this token represents an assertion that AWS can evaluate. If your IAM trust policy checks only the `iss` (Issuer) and `aud` (Audience) claims, then **any** JWT issued by GitHub Actions will be accepted. AWS STS will successfully issue credentials to your runner, regardless of which GitHub repository or account triggered the execution.

## The Wildcard Trust Anti-pattern

The most common OIDC misconfiguration is the "Wildcard Trust." In a rush to eliminate static access keys, platforms-engineering teams often deploy a single OIDC IAM role with a trust policy that permits access based on a broad repository wildcard.

Consider this vulnerable trust policy:

<script src="https://gist.github.com/mohashari/54fac57f10040c9b2cf7e85b7f541665.js?file=snippet-2.json"></script>

This policy contains a critical security flaw. The condition checking the subject claim (`sub`) matches `repo:my-organization/*:*`. While this successfully blocks other GitHub organizations from assuming the role, it trusts **every single repository** within `my-organization`. 

If a junior developer creates a public scratch repository to test an open-source tool, or if a legacy repository with lax branch protection rules is compromised, an attacker can write a workflow in that repository, reference your production IAM Role ARN, and immediately obtain administrative control over your cloud environment. 

A hardened policy must limit access to a specific repository, a specific environment, or a specific branch.

## Hardening Trust Policies: Branch, Repository, and Environment Scoping

To implement a zero-trust architecture, you must structure your IAM trust policies to enforce multiple constraints simultaneously. 

### 1. Repository-Level Isolation
Each workload must have its own dedicated IAM role. A microservice deploying to AWS should only be assumed by its corresponding GitHub repository.

### 2. Environment-Level Constraint
The subject claim (`sub`) changes structure based on whether the GitHub Action run targets a defined GitHub Environment. If the job is bound to an environment named `production`, the subject claim takes the form `repo:<org>/<repo>:environment:production`. 

By scoping the trust policy to the environment, you can leverage GitHub’s environment protection rules. For instance, you can configure GitHub to require manual approval from lead engineers before executing any workflow targeting the `production` environment. Because the OIDC token is only minted *after* these gates are passed, AWS STS enforces a cryptographic verification of your human deployment approval process.

Here is the Terraform configuration to declare a hardened OIDC provider and a secure, environment-constrained IAM deployment role:

<script src="https://gist.github.com/mohashari/54fac57f10040c9b2cf7e85b7f541665.js?file=snippet-3.hcl"></script>

## Configuring the GitHub Actions Workflow for Zero-Trust

Once the IAM role is defined, you must structure the GitHub Actions workflow to safely negotiate the OIDC handshake. There are two critical configurations to enforce within the workflow YAML file:

### 1. Explicit Privilege Scoping
By default, GitHub runs have a default token permissions block that can vary depending on repository settings. You must explicitly configure the `permissions` block to limit risks. The workflow requires `id-token: write` to retrieve the JWT from GitHub's OIDC provider. You should drop all other permissions to the absolute minimum required, typically `contents: read` to checkout code.

### 2. Immutable Run Traceability
When using the `aws-actions/configure-aws-credentials` action, set the `role-session-name` parameters using GitHub run metadata. By including `${{ github.run_id }}` and `${{ github.run_attempt }}` in the session name, you emit an audit trail into AWS CloudTrail that maps directly back to the specific execution run in the GitHub UI.

<script src="https://gist.github.com/mohashari/54fac57f10040c9b2cf7e85b7f541665.js?file=snippet-4.yaml"></script>

## Multi-Account AWS Architecture: Landing Zones & Role Chaining

In enterprise environments with hundreds of AWS accounts managed via AWS Organizations, placing OIDC provider definitions in every target account is an operational anti-pattern. If GitHub shifts its certificate authority or configuration, platform teams are forced to update hundreds of provider resources across multiple environments.

The industry-standard pattern is to use a **Hub-and-Spoke Identity Model**:
1. Deploy the OIDC Provider (`aws_iam_openid_connect_provider`) in a single, centralized **Hub Account** (e.g., an Identity or Shared Services account).
2. Create a generic "Hub Role" that GitHub Actions assumes directly using `AssumeRoleWithWebIdentity`.
3. Configure the Hub Role to use AWS Session Tags (Principal Tags) to preserve OIDC metadata.
4. Have the Hub Role call `sts:AssumeRole` to hop into target roles in the **Spoke Accounts** (Dev, Staging, Prod).

### Attributing and Forwarding OIDC Claims via ABAC
To make role chaining secure, we use Attribute-Based Access Control (ABAC). When assuming the Hub Role, AWS allows us to map JWT claims like `repository` and `environment` into transient session tags. When the Hub Role attempts to assume a role in a Spoke account, we can enforce a policy that ensures the Spoke account role can only be assumed if the incoming session tags match the target workload.

Here is the IAM policy for the **Spoke Account** role, permitting assumption *only* if the Hub role has been assumed by the designated repository and environment:

<script src="https://gist.github.com/mohashari/54fac57f10040c9b2cf7e85b7f541665.js?file=snippet-5.json"></script>

This prevents the Hub Role from being abused. Even if an attacker compromises a workflow in another repository (`microservice-b`) and tries to run a step that assumes the production deployment role for `microservice-a`, AWS STS blocks the request at the spoke account boundary because the session tags mapped from the GitHub JWT do not match `microservice-a`.

## Monitoring, Auditing, and Detection of Abuse

Security configuration is only as good as your visibility. When an OIDC token is used to assume an IAM role, AWS records an `AssumeRoleWithWebIdentity` event in AWS CloudTrail. 

You must continuously monitor these events. Specifically, pay attention to the `additionalEventData` block. When GitHub acts as the OIDC provider, AWS extracts and logs key claims directly inside this block.

Use Amazon Athena to query CloudTrail logs and audit the usage of your federated roles:

<script src="https://gist.github.com/mohashari/54fac57f10040c9b2cf7e85b7f541665.js?file=snippet-6.sql"></script>

### Warning Signals to Alert On
* **Mismatched Session Names**: If your workflow standardizes session names to `gh-run-${{ github.run_id }}` and you detect a session named `my-custom-session` or `admin`, alert immediately. This suggests an attacker is passing manual inputs or using a custom execution path.
* **Geographical Anomaly**: GitHub Action runners run on Azure/GitHub-owned IP ranges. If an `AssumeRoleWithWebIdentity` call originates from a residential IP block or an unaligned cloud provider, it indicates a compromised token is being replayed off-platform.
* **Untrusted Repositories**: If the Athena query exposes subject claims targeting repositories outside your organization (e.g. personal forks), it implies a configuration leak in your OIDC trust definition.

## Operationalizing Rotation and Policy Enforcement

Moving to OIDC does not mean you can ignore policy management. You should establish automated guardrails to ensure that no developer can bypass these security measures.

Use AWS Organizations Service Control Policies (SCPs) to systematically prevent the creation of static IAM access keys in your production accounts. This enforces OIDC as the only viable mechanism for pipeline deployment:

<script src="https://gist.github.com/mohashari/54fac57f10040c9b2cf7e85b7f541665.js?file=snippet-7.json"></script>

By enforcing this guardrail, you guarantee that no static keys can be created, eliminating the risk of credential leakage. Meanwhile, your hardened, environment-scoped OIDC trust policies ensure that only cryptographically verified, human-approved runs in specific GitHub Actions workflows can touch your production environments.