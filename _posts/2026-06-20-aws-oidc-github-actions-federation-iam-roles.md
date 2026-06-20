---
layout: post
title: "Implementing OIDC-Based AWS IAM Federation for GitHub Actions Without Long-Lived Credentials"
date: 2026-06-20 08:00:00 +0700
tags: [aws, github-actions, oidc, security, devsecops]
description: "Eliminate long-lived AWS IAM access keys in your CI/CD pipelines by implementing keyless OIDC-based federation with GitHub Actions."
image: "/images/diagrams/aws-oidc-github-actions-federation-iam-roles.svg"
thumbnail: "/images/diagrams/aws-oidc-github-actions-federation-iam-roles.svg"
---

Storing long-lived AWS IAM access keys in GitHub repository secrets is a high-severity incident waiting to happen. In typical production environments, security teams frequently find themselves auditing compromised pipelines, cleaning up public S3 buckets, and responding to five-figure AWS bills run up by cryptominers who harvested a leaked `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` pair. Once these static credentials are saved as secrets, they are vulnerable to exposure via accidental echo statements in debugging scripts, malicious pull request modifications by external contributors, or breaches of third-party CI dependency packages. Furthermore, complying with modern compliance standards like SOC 2, PCI-DSS, or ISO 27001 requires rotating credentials every 90 days or less—a task that is often neglected because it manualizes CI/CD maintenance and risks breaking active build scripts. The industry-standard solution is to completely eliminate static credentials in favor of keyless OpenID Connect (OIDC) federation. By configuring AWS Identity and Access Management (IAM) to trust GitHub’s OIDC Identity Provider (IdP), GitHub Actions runners can dynamically request short-lived, cryptographically signed JSON Web Tokens (JWTs) and exchange them via AWS Security Token Service (STS) for temporary AWS credentials that automatically expire after a maximum of one hour.

![Implementing OIDC-Based AWS IAM Federation for GitHub Actions Without Long-Lived Credentials Diagram](/images/diagrams/aws-oidc-github-actions-federation-iam-roles.svg)

## The Mechanics of OIDC Federation with AWS STS

Federated authentication between GitHub Actions and AWS relies on an asymmetric cryptographic trust relationship. The workflow removes the necessity of storing any secret inside GitHub, relying instead on a sequence of API handshakes:

1. **Token Generation**: When a GitHub Actions workflow starts, the runner contacts GitHub's internal token service and requests an OIDC token containing specific metadata (claims) about the current run, such as the repository name, runner actor, branch name, and commit SHA.
2. **OIDC Signature**: The GitHub token service signs this payload with its private key and issues a JWT token.
3. **STS Exchange**: The runner invokes the AWS Security Token Service (STS) `AssumeRoleWithWebIdentity` API, passing the JWT and the target IAM Role ARN.
4. **Validation and Trust Verification**: AWS STS intercepts the request. It fetches GitHub’s public JSON Web Key Set (JWKS) from `https://token.actions.githubusercontent.com/.well-known/jwks.json` (or uses a cached version) to verify that the token's cryptographic signature is authentic.
5. **Claims Evaluation**: STS evaluates the verified claims inside the JWT against the target IAM Role’s trust policy. It checks that the token’s issuer (`iss`) matches GitHub, the audience (`aud`) matches `sts.amazonaws.com`, and the subject (`sub`) matches the specific GitHub repository and branch allowed by the role.
6. **Credential Issuance**: If the policies align, STS returns temporary security credentials (access key, secret key, and session token) to the runner. The credentials automatically expire within a default window of 1 hour, rendering them useless if exfiltrated after the pipeline run completes.

## Establishing the AWS Identity Provider for GitHub

To implement this workflow, you must first register GitHub as an OpenID Connect Identity Provider within your AWS account. This registration tells IAM to trust tokens issued by GitHub's domain name. 

The following Terraform configuration defines the OIDC provider. While AWS IAM historically required hardcoded thumbprints of the GitHub OIDC root certificate authority (CA) to prevent man-in-the-middle attacks, AWS STS now automatically manages certificate validation against Amazon's trust store. However, the IAM API still requires a thumbprint list to be sent during provider creation. We pass the SHA-1 thumbprint of the root CA (`6938fd4d98bab03faadb97b34396831e3780aea1`) to satisfy the API.

<script src="https://gist.github.com/mohashari/5316d7b0b35d22f80e5e74b343e8c678.js?file=snippet-1.txt"></script>

## Designing a Secure IAM Role Trust Policy

The core of your OIDC security posture resides in the IAM Role’s trust relationship policy. If your trust policy is too permissive—for instance, using wildcards that accept any token issued by GitHub—any user with a GitHub account can assume your IAM role and access your AWS resources. 

To prevent cross-organization tenant attacks, your trust policy must restrict access based on the `token.actions.githubusercontent.com:sub` (subject) claim. The subject claim contains the repository path and the Git reference triggering the workflow. In the configuration below, we enforce that only workflows running inside the `mohashari/production-deploy-service` repository can assume this role. We also use a conditional rule to ensure the audience (`aud`) matches `sts.amazonaws.com`.

<script src="https://gist.github.com/mohashari/5316d7b0b35d22f80e5e74b343e8c678.js?file=snippet-2.txt"></script>

## Orchestrating the GitHub Actions Workflow

To allow the GitHub Actions runner to negotiate with the OIDC provider, your workflow configuration file must be explicitly granted token writing privileges. By default, GitHub Actions runs jobs with read-only scopes. You must declare `permissions: id-token: write` in either the global workflow scope or the specific deployment job. Without this permission, the GitHub runner agent will not generate the `ACTIONS_ID_TOKEN_REQUEST_TOKEN` environment variable, causing STS authentication calls to fail with token initialization errors.

We utilize the official `aws-actions/configure-aws-credentials` action to execute the credential exchange. It parses the environment, pulls the signed JWT from GitHub's runtime API, communicates with AWS STS, and exports the resulting temporary credentials to the runner's shell environment.

<script src="https://gist.github.com/mohashari/5316d7b0b35d22f80e5e74b343e8c678.js?file=snippet-3.yaml"></script>

## Anatomy of the GitHub Actions OIDC JWT Payload

To construct accurate and secure trust policies, you need to understand the structural properties of the JWT token issued by GitHub. The token is a standard RFC 7519 payload containing claims metadata. 

When AWS STS parses the token, it validates the structure and evaluates the claim mappings against the conditions in the IAM trust policy. The example JSON block below shows a decoded payload from a GitHub workflow run. The `sub` field is particularly important: it consolidates the org name, repo name, git reference, and event type. Notice the presence of the `job_workflow_ref`, which can be validated to ensure the code was built by a specific deployment template rather than an ad-hoc runner script.

<script src="https://gist.github.com/mohashari/5316d7b0b35d22f80e5e74b343e8c678.js?file=snippet-4.json"></script>

## Debugging OIDC Claim Mismatch and Retrieval Failures

A recurring failure mode in production is the cryptic error:
`AccessDenied: An error occurred (AccessDenied) when calling the AssumeRoleWithWebIdentity operation: Not authorized to perform AssumeRoleWithWebIdentity`.

This occurs because of a mismatch between the claims in the JWT token and the conditions written in the AWS IAM trust policy. Because GitHub rotates token values dynamically, diagnosing these mismatches requires extracting the raw token inside the pipeline container, decoding it, and comparing it manually against your Terraform rules.

The Bash script below can be run within a workflow step to extract the GitHub Actions JWT token directly from the local runner agent's service port, decode the Base64 payload, and print the parsed JSON claims for troubleshooting.

<script src="https://gist.github.com/mohashari/5316d7b0b35d22f80e5e74b343e8c678.js?file=snippet-5.sh"></script>

## Custom Runner Access and AWS SDK Integration

If your CI/CD workflow invokes compiled backend binaries (e.g., custom deployment scripts, schema migrations, or artifact upload utilities), you do not need to rely on the wrapper AWS CLI to assume roles. The AWS SDK natively supports OIDC token assumption via standard environment variables: `AWS_ROLE_ARN`, `AWS_WEB_IDENTITY_TOKEN_FILE`, and `AWS_REGION`.

When using the `aws-actions/configure-aws-credentials` action, it writes the retrieved web identity token to a file on the runner container's disk and sets these variables. Any downstream process written in Go, Python, or Java will automatically detect these configurations and execute the STS handshake implicitly without manual developer integration.

The Go application snippet below demonstrates how the AWS SDK V2 automatically loads configuration and uses the web identity token path to establish authenticated client contexts.

<script src="https://gist.github.com/mohashari/5316d7b0b35d22f80e5e74b343e8c678.js?file=snippet-6.go"></script>

## Critical Production Failure Modes and Mitigation Strategies

While implementing OIDC-based IAM federation eliminates static credential storage, running this setup in high-throughput enterprise pipelines exposes unique operational limits and failure vectors.

### Failure Mode 1: Branch Naming and Subject Claim Mismatches
When teams transition to GitFlow or trunk-based development with short-lived feature branches, they often find their pipelines breaking with `AccessDenied` errors. If your trust policy condition uses a strict equality test like `"StringEquals"` on the `sub` claim (as in snippet-2) mapping only to `repo:mohashari/production-deploy-service:ref:refs/heads/main`, then developers attempting to run test deployments or infrastructure validation steps from feature branches (e.g., `ref:refs/heads/feature/add-rds-read-replica`) will be locked out.

**The Mitigation**: You must separate deployments to staging and production by defining distinct IAM Roles, each configured with target conditions matching specific Git refs. For example, assign a read-only testing role to feature branches using wildcard operators, and reserve the write-privileged role for the main branch.

<script src="https://gist.github.com/mohashari/5316d7b0b35d22f80e5e74b343e8c678.js?file=snippet-7.txt"></script>

### Failure Mode 2: Global STS Endpoint Throttling in High-Volume Workflows
By default, the `configure-aws-credentials` action communicates with the global AWS STS endpoint (`https://sts.amazonaws.com`). In high-volume software engineering organizations executing hundreds of CI/CD pipeline builds concurrently, you can quickly hit API request limits on the global STS namespace, triggering throttling alerts like `Rate exceeded (503 Service Unavailable)` on your builds.

**The Mitigation**: Route authentication requests to regional AWS STS endpoints, which have higher quota limits and lower network latency. To implement this, pass the regional configuration switch (`AWS_STS_REGIONAL_ENDPOINTS: regional`) to your runner container's environment variables.

<script src="https://gist.github.com/mohashari/5316d7b0b35d22f80e5e74b343e8c678.js?file=snippet-8.yaml"></script>

### Failure Mode 3: OIDC Clock Drift in Self-Hosted Runner Infrastructure
If you operate self-hosted GitHub Actions runners inside a Kubernetes cluster or on private EC2 instances, you will periodically encounter random authentication rejections. AWS STS enforces a strict validation of the OIDC token’s expiration (`exp`) and not-before (`nbf`) claims. If your runner VM's system clock drifts by more than 5 minutes relative to the GitHub token generator time, AWS will reject the token handshake as expired or not yet valid.

**The Mitigation**: Install and configure an NTP synchronization daemon (such as `chronyd` or `systemd-timesyncd`) on your host VM images to guarantee clock skew remains under 100 milliseconds.

## Conclusion

Transitioning to OIDC-based federation is the single most impactful security enhancement you can apply to your AWS-integrated CI/CD pipelines. By establishing a direct cryptographic trust between GitHub's Identity Provider and AWS STS, you completely eliminate the security risk of compromised access keys, simplify audit logs, and automate rotation compliance. Designing strict trust policies that filter by specific repository refs, troubleshooting claim matches with direct JWT extraction scripts, and utilizing regional STS endpoints will ensure your infrastructure remains secure, observable, and highly available.