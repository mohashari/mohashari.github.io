---
layout: post
title: "Preventing Credential Leakage: Automated Just-In-Time AWS IAM Session Token Generation in GitHub Actions using OIDC and Vault"
date: 2026-07-03 08:00:00 +0700
tags: [devsecops, aws, security, vault, github-actions]
description: "Eliminate static AWS keys in CI/CD. Learn how to implement secure, automated, just-in-time AWS session token generation using GitHub OIDC and HashiCorp Vault."
image: "https://picsum.photos/seed/3619/1080/720"
thumbnail: "https://picsum.photos/seed/3619/400/300"
---

Consider the following incident: an upstream npm package used in your CI pipeline is compromised. During a routine build, a malicious payload runs, scans the environment variables, and exfiltrates `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` to an anonymous pastebin. If you are using static, long-lived AWS IAM user credentials stored in GitHub Secrets, the blast radius is indefinite. An attacker has immediate, persistent access to your AWS infrastructure until someone manually notices the leak, rotates the keys, and updates the repository secrets. Even with active monitoring, the detection-to-remediation window can take hours—more than enough time for an attacker to spin up expensive GPU instances, copy sensitive databases, or compromise production clusters.

To eliminate this class of vulnerability entirely, production environments must adopt a zero-trust model for CI/CD authentication. By combining GitHub's OpenID Connect (OIDC) identity provider with HashiCorp Vault and the AWS Secrets Engine, we can generate dynamic, short-lived AWS IAM session tokens that are valid only for the duration of a specific job step. If a token is exfiltrated, it expires automatically within minutes, rendering it useless to external attackers. 

## The Architectural Blueprint: Zero-Trust CI/CD Federation

While AWS offers direct OIDC federation using the `configure-aws-credentials` action, routing this handshake through HashiCorp Vault as an intermediary agent provides substantial advantages for enterprise environments. It establishes a centralized authentication gateway, enforcing unified role-based access control (RBAC), multi-cloud federation, and mandatory audit logging across all platforms (AWS, GCP, Azure, and Kubernetes).

The trust negotiation flows through five main steps:

1. **OIDC Handshake**: The GitHub Actions runner requests a cryptographically signed JSON Web Token (JWT) from GitHub's OIDC provider.
2. **Vault Authentication**: The runner presents this JWT to HashiCorp Vault's JWT/OIDC authentication backend.
3. **Identity Verification**: Vault validates the JWT signature against GitHub's public keys (JWKS) and verifies the payload claims (such as repository name, workflow, branch, and run ID).
4. **Token Exchange**: Once verified, Vault maps the GitHub identity to a specific Vault role. The runner then queries Vault's AWS secrets engine.
5. **AWS STS Generation**: Vault interacts with the AWS Security Token Service (STS) via an assumed role to generate a dynamic, short-lived IAM session token, returning it to the GitHub Actions runner.

```
+------------------+       1. Request JWT       +-------------------+
|  GitHub Actions  | -------------------------> |    GitHub OIDC    |
|      Runner      | <------------------------- |     Provider      |
+------------------+       Signed JWT (OIDC)    +-------------------+
        |
        | 2. Authenticate with JWT
        v
+------------------+       3. Verify Token      +-------------------+
|                  | -------------------------> | GitHub JWKS Keys  |
| HashiCorp Vault  | <------------------------- | (Public Keys API) |
|                  |       Signature Valid      +-------------------+
+------------------+
        |
        | 4. Call AWS STS
        v
+------------------+       5. Return STS Token  +------------------+
|  AWS STS Engine  | -------------------------> |  Target AWS API  |
|     (IAM)        |                            | (JIT Access)     |
+------------------+                            +------------------+
```

## Step 1: Configuring GitHub OIDC Trust in HashiCorp Vault

First, Vault must be configured to trust JWTs issued by GitHub. This is achieved by enabling the JWT authentication method and pointing it to GitHub's OpenID Connect provider configuration endpoint.

The following Terraform configuration initializes the Vault JWT auth backend, registers GitHub's OIDC issuer URL, and establishes the validation framework.

<script src="https://gist.github.com/mohashari/146e50a5d8989c5f8eeee8c7c80449dd.js?file=snippet-1.hcl"></script>

The `oidc_discovery_url` directs Vault to query `https://token.actions.githubusercontent.com/.well-known/openid-configuration` to fetch the JSON Web Key Set (JWKS) public keys. These keys are used to cryptographically verify that incoming JWTs were signed by GitHub's infrastructure and have not been altered in transit.

## Step 2: Restricting Access Using OIDC Claim Mapping

Establishing trust with GitHub's OIDC provider is only the first step. You must also prevent any arbitrary GitHub user from authenticating against your Vault instance. This is accomplished by defining a Vault role with strict `bound_claims`.

When GitHub Actions generates an OIDC token, it includes metadata claims about the executing environment:
* `repository`: The owner and name of the repository (e.g., `owner/repo`).
* `ref`: The branch or tag that triggered the run (e.g., `refs/heads/main`).
* `actor`: The GitHub username that initiated the workflow.

Here is how you map a specific production deployment pipeline to a Vault authentication role:

<script src="https://gist.github.com/mohashari/146e50a5d8989c5f8eeee8c7c80449dd.js?file=snippet-2.hcl"></script>

By enforcing these `bound_claims`, Vault will reject any token where the repository is not `my-organization/production-app` or where the code is executing on a branch other than `main`. This prevents unauthorized developers, feature branches, or malicious pull requests in forked repositories from obtaining production privileges.

## Step 3: Configuring the Vault AWS Secrets Engine

Vault's AWS Secrets Engine must be configured to issue dynamic credentials. Instead of using static IAM users, we use IAM roles assumed through Vault.

First, enable the AWS secrets engine in Vault and configure the root credentials Vault will use to interact with AWS. In a production environment, Vault should run on an AWS EC2 instance or EKS cluster utilizing an IAM Instance Profile to avoid storing static root access keys inside Vault.

<script src="https://gist.github.com/mohashari/146e50a5d8989c5f8eeee8c7c80449dd.js?file=snippet-3.hcl"></script>

The configuration sets `credential_type = "assumed_role"`. When the runner requests credentials, Vault calls the AWS STS `AssumeRole` API on behalf of the client, generating a dynamic access key, secret key, and session token.

## Step 4: The Target AWS IAM Role and Policy Boundary

The target AWS IAM Role (`GithubActionsDeploymentRole`) must have a Trust Policy that allows Vault's IAM entity to assume it. Additionally, you must apply a Policy Boundary to limit the maximum permissions a dynamic session can obtain, protecting against privilege escalation.

Below is the Trust Policy definition for the AWS IAM role.

<script src="https://gist.github.com/mohashari/146e50a5d8989c5f8eeee8c7c80449dd.js?file=snippet-4.json"></script>

The trust policy ensures that only your specific Vault server, identified by its instance role `VaultServerIAMRole` and backed by a unique `ExternalId`, can assume the deployment role.

Next, we define the IAM Policy Boundary. Even if a developer configures the GitHub Actions workflow to request administrator privileges, the AWS session will be constrained by this boundary.

<script src="https://gist.github.com/mohashari/146e50a5d8989c5f8eeee8c7c80449dd.js?file=snippet-5.json"></script>

Applying this boundary guarantees that even in the event of a total compromise of the CI pipeline, the attacker cannot create new IAM users, attach administrator policies, or delete logging and auditing resources.

## Step 5: Building the GitHub Actions Workflow

With Vault and AWS configured, you can now construct the GitHub Actions workflow. The workflow runner must first acquire a JWT from GitHub's OIDC provider.

To allow the workflow to obtain this OIDC token, you must grant the job the `id-token: write` permission. This permission is disabled by default for security reasons.

<script src="https://gist.github.com/mohashari/146e50a5d8989c5f8eeee8c7c80449dd.js?file=snippet-6.yaml"></script>

### Explaining the GitHub runner shell script mechanics:
1. **GitHub OIDC Token Fetching**: The runner interacts with the local loopback endpoint configured by GitHub (`$ACTIONS_ID_TOKEN_REQUEST_URL`) using the temporary authentication token (`$ACTIONS_ID_TOKEN_REQUEST_TOKEN`). We specify the audience parameter matching the Vault server's base domain to prevent replay attacks on other systems.
2. **Vault Login**: The runner posts the OIDC JWT to the `/v1/auth/github-actions/login` endpoint to exchange it for a Vault client token.
3. **AWS Credential Retrieval**: Using the Vault client token as the authorization header, the runner reads the `/v1/aws-production/creds/app-deploy-role` path to get dynamic AWS STS tokens.
4. **GitHub Output Masking**: The script uses GitHub Actions workflow commands (`::add-mask::<value>`) to intercept any stdout printing of these credentials. This ensures that even if a subsequent step runs `env` or prints a debug trace, the secret values will appear as asterisks (`***`) in the GitHub logs.

## Hardening the Production Pipeline: Failure Modes & Mitigation

Designing a zero-trust architecture requires anticipating failure modes. The following sections outline common operational vulnerabilities and their configurations.

### 1. The "Dirty Branch" Attack
A developer creates a branch named `main/patch` or a pull request targeting `main`. If the OIDC rule checks only `ref == "refs/heads/main"`, a malformed rule could match sub-patterns. To prevent this, enforce exact regular expression checks or use strict mapping rules on Vault's claims. 

You must configure the JWT claim mapping in Vault to ensure that the branch name matches the exact string. Avoid wildcard structures like `refs/heads/main*` in production role definitions.

### 2. DNS or Network Resolution Issues
Since the GitHub runner needs to communicate directly with your HashiCorp Vault instance, Vault must be reachable from the public internet or via a secure tunnel. If your Vault instance is private and nested inside a VPC, the runner will fail to connect.

To mitigate this, you have three options:
* **Self-Hosted Runners**: Deploy GitHub Actions self-hosted runners within your private AWS VPC. This allows runners to communicate with Vault over internal VPC endpoints.
* **Vault Gateway**: Expose only the Vault JWT login path (`/v1/auth/github-actions/login`) via an ALB or API Gateway with strict IP throttling policies.
* **OIDC Direct Federation**: If Vault connectivity cannot be secured, use AWS IAM OIDC directly as a backup mechanism, utilizing IAM Session Policies to constrain permissions.

### 3. Verification Script for Leased Credential Expiration
To verify that lease limits are being strictly enforced in production, engineers can run an out-of-band monitoring daemon. The following Python script connects to AWS STS to evaluate the remaining lifetime of the active CI/CD session.

<script src="https://gist.github.com/mohashari/146e50a5d8989c5f8eeee8c7c80449dd.js?file=snippet-7.py"></script>

## Conclusion

Securing CI/CD pipelines requires moving away from static, long-lived credentials. Transitioning to GitHub Actions OIDC and HashiCorp Vault creates a system where credentials are generated dynamically on demand, scoped to specific workflows, and automatically revoked when no longer needed. If a pipeline is compromised, the exposed credentials will expire within minutes, significantly limiting the potential damage.

Implementing this configuration removes the administrative overhead of rotating static secrets and ensures that your deployment pipelines comply with modern security standards.