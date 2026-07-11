---
layout: post
title: "Hardening CI/CD Pipelines: Ephemeral Self-Hosted Runner Orchestration on AWS EC2 with GitHub Actions OIDC and HashiCorp Vault"
date: 2026-07-11 08:00:00 +0700
tags: [devsecops, aws, security, vault, github-actions]
description: "Build a zero-trust, ephemeral self-hosted GitHub Actions runner architecture on AWS EC2 using GitHub OIDC and HashiCorp Vault to eliminate static keys."
image: "/images/diagrams/hardening-cicd-ephemeral-self-hosted-runners-aws-actions-oidc-vault.svg"
thumbnail: "/images/diagrams/hardening-cicd-ephemeral-self-hosted-runners-aws-actions-oidc-vault.svg"
---

Consider a standard enterprise CI/CD setup: persistent self-hosted runners running on EC2 instances inside a private VPC, executing build jobs sequentially day after day. A single compromised third-party package dependency or a malicious pull request from an internal developer is all it takes to poison the entire pipeline. Because the runner is persistent, the malware can write a backdoor into the shared Docker socket, exfiltrate static AWS IAM credentials stored in `~/.aws/credentials`, or hijack the runner's IAM instance profile to modify production S3 buckets. Static, long-lived credentials stored in GitHub Secrets represent a massive, fragile target; once leaked, they are rarely rotated within the standard five-minute window that automated scripts use to exploit them. The only robust solution to this threat vector is to move to an ephemeral, zero-trust runner model. In this architecture, runner VMs exist only for the duration of a single job, and they possess no long-lived credentials. Instead, they authenticate to AWS and HashiCorp Vault dynamically on a per-job basis using short-lived OpenID Connect (OIDC) JSON Web Tokens (JWT) issued by GitHub Actions.

![Hardening CI/CD Pipelines: Ephemeral Self-Hosted Runner Orchestration on AWS EC2 with GitHub Actions OIDC and HashiCorp Vault Diagram](/images/diagrams/hardening-cicd-ephemeral-self-hosted-runners-aws-actions-oidc-vault.svg)

## The Threat Landscape of Long-Lived CI/CD Runners

Traditional self-hosted runner deployments suffer from three primary structural security flaws: persistent state contamination, excessive privilege accumulation, and static credential leaks. 

In a persistent runner setup, a build script can easily execute a daemon or leave a cron job behind in the runner's operating system. Because successive jobs run on the same virtual machine, a compromised build in a test repository can inject malicious code into a production build scheduled on the same runner hours later. Sharing the `/var/run/docker.sock` to enable Docker-in-Docker (DinD) builds exacerbates this, giving any build job root access to the host operating system.

Furthermore, teams frequently assign static IAM instance profiles to EC2 runners to facilitate deployments. This means a vulnerability in a test branch has the exact same permissions to deploy to production as a release branch. If you avoid instance profiles by storing static IAM access keys in GitHub Secrets, you create a static secret sprawl. If an attacker exfiltrates these keys via a simple `echo $AWS_SECRET_ACCESS_KEY | base64` command in a PR build, your entire AWS cloud footprint is vulnerable.

An ephemeral, zero-trust orchestration model solves these flaws by enforcing two tenets:
1. **Infrastructure Ephemerality:** Every GitHub Actions job executes on a clean, dedicated EC2 instance running a hardened AMI. The instance is provisioned on demand and terminates immediately upon completing a single job (`run.sh --once`).
2. **Keyless Identity Exchange:** The runner has no static credentials. At runtime, the runner requests a cryptographically signed OIDC token from GitHub's identity provider, exchanges it with HashiCorp Vault for short-lived credentials, uses them, and exits. The credentials expire automatically within minutes.

## Zero-Trust Architecture: The Ephemeral Loop

The architecture consists of a reactive control loop triggered by GitHub Actions webhook events:
* **The Webhook Listener:** A lightweight orchestrator service runs in your VPC, listening for `workflow_job.queued` events.
* **The Provisioner:** Upon receiving a valid webhook, the orchestrator issues a call to the AWS EC2 API to spin up a single-use EC2 instance (Spot or On-Demand) pre-configured with a hardened runner image.
* **The Runner Lifecycle:** The instance boots, registers with GitHub using a short-lived registration token, executes the single queued job, and terminates itself immediately.
* **The Secret Exchange:** During execution, the runner uses GitHub’s OIDC provider to authenticate to HashiCorp Vault. Vault validates the claims on the OIDC token (verifying the repository name, branch, and actor) and issues a short-lived Vault token or dynamic AWS credentials.

Let's walk through implementing this pipeline step-by-step.

## Step 1: Establishing the OIDC Trust on HashiCorp Vault

First, we must configure HashiCorp Vault to trust JWTs issued by GitHub Actions. Vault uses GitHub's public OpenID Connect discovery endpoint (`token.actions.githubusercontent.com`) to fetch the JSON Web Key Set (JWKS) required to cryptographically verify the signatures of incoming GitHub tokens.

We use Terraform to define the JWT auth backend and map the identity provider to Vault.

<script src="https://gist.github.com/mohashari/8ee5f6a5cddac51e5fcbc5da1f1ffccc.js?file=snippet-1.hcl"></script>

This configuration establishes the cryptographic handshake. Any token generated by GitHub Actions can now be presented to Vault's `/v1/auth/github-actions/login` endpoint for verification.

## Step 2: Building the Go Webhook Orchestrator

The orchestrator sits at the entry point of our infrastructure. It must be highly secure, validating the HMAC-SHA256 signature of every incoming GitHub webhook payload using a secret shared between your GitHub App/Organization and the service. 

When a job labeled `aws-ephemeral` is queued, the orchestrator parses the payload and launches an EC2 instance. The Go snippet below illustrates this verification and provisioning logic:

<script src="https://gist.github.com/mohashari/8ee5f6a5cddac51e5fcbc5da1f1ffccc.js?file=snippet-2.go"></script>

The orchestrator must run under an IAM role on AWS that possesses strictly restricted `ec2:RunInstances` privileges, limited to a specific subnet, security group, and AMI ID.

## Step 3: Hardening the Runner OS and the User Data Boot Sequence

The EC2 instance is created with a User Data script that configures the operating system, registers the runner with GitHub, executes the job *exactly once*, and schedules termination. 

To maintain security:
* Run the GitHub Actions Runner agent under a non-root system user (`runner`).
* Configure rootless Docker so container breakouts do not yield root access to the host kernel.
* Run the agent with the `--once` flag. This guarantees that once the assigned job finishes, the runner daemon shuts down.
* Terminate the instance at the OS level immediately following the runner's exit.

<script src="https://gist.github.com/mohashari/8ee5f6a5cddac51e5fcbc5da1f1ffccc.js?file=snippet-3.sh"></script>

## Step 4: Vault Integration via GitHub OIDC inside the Pipeline

Now that our runner is ephemeral, how does the runner job safely retrieve secrets from HashiCorp Vault without hardcoding passwords? 

When the workflow begins on our self-hosted runner, we request an OIDC JWT from GitHub's identity provider. This token contains standard claims about the running job. We then use Vault’s official GitHub action to authenticate.

<script src="https://gist.github.com/mohashari/8ee5f6a5cddac51e5fcbc5da1f1ffccc.js?file=snippet-4.yaml"></script>

The OIDC JWT is generated dynamically by the runner environment and sent directly to Vault's endpoint via HTTPS. The token has an default lifespan of just 5 minutes, mitigating the risk of replay attacks.

## Step 5: Advanced JWT Claim Mapping for Tenant Isolation

A major vulnerability in naive OIDC implementations is configuration wildcarding. If your Vault OIDC backend accepts *any* token from `token.actions.githubusercontent.com` without claim constraints, any public GitHub repository can authenticate to your Vault.

To prevent this, you must configure a Vault JWT Role that enforces strict boundaries on claims:
* `iss` (Issuer): Must match `https://token.actions.githubusercontent.com`.
* `aud` (Audience): Must match your Vault's URL.
* `sub` (Subject): Restricts execution to specific organizations, repositories, and environments.
* `bound_claims`: Enforce restrictions on specific keys in the token payload.

<script src="https://gist.github.com/mohashari/8ee5f6a5cddac51e5fcbc5da1f1ffccc.js?file=snippet-5.hcl"></script>

By specifying the `repository` and `ref` bound claims, we guarantee that developer testing branches or other repositories in the organization cannot assume the roles necessary to pull production database keys.

## Operationalizing at Scale: Failure Modes and Mitigations

When deploying ephemeral orchestration in production, simple script errors can lead to major operational disruption. Here are the three primary production failure modes and how to mitigate them:

### 1. API Rate Limiting (AWS and GitHub)
If you run high-frequency pipelines, your orchestrator will execute hundreds of EC2 `RunInstances` API calls. AWS limits API mutation rates, and you will eventually encounter `RequestLimitExceeded` errors.
* **Mitigation:** Wrap the orchestrator client with exponential backoff and jitter. Implement a Redis-backed queue inside your orchestrator to pool run requests and serialize them rather than executing Go routines directly on webhooks.

### 2. Zombie Runner VM Leakage
An EC2 runner may spin up, but fail to boot or download the actions-runner package due to network timeouts, VPC route failures, or DNS outages. Because the registration step never happens, GitHub Actions cannot execute the job, and the VM never hits the `./run.sh` script, leaving it running indefinitely. This leads to massive cost overruns.
* **Mitigation:** Build a decoupled sweeper script that runs as a cron job inside your infrastructure. It queries AWS for ephemeral runner instances and terminates any instance that has been running for longer than an hour without registering.

The following Python script implements this sweeper logic using `boto3`:

```python
# snippet-6
import boto3
from datetime import datetime, timedelta, timezone

def lambda_handler(event, context):
    """
    Sweeper to terminate orphaned self-hosted runners that failed to clean up.
    Runs via EventBridge rule every 15 minutes.
    """
    ec2 = boto3.client('ec2')
    
    # Locate all instances spawned by the orchestrator
    filters = [
        {'Name': 'tag:Role', 'Values': ['ephemeral-runner']},
        {'Name': 'instance-state-name', 'Values': ['running']}
    ]
    
    response = ec2.describe_instances(Filters=filters)
    instances_to_terminate = []
    
    now = datetime.now(timezone.utc)
    max_lifespan = timedelta(hours=1) # Hard limit for run time
    
    for reservation in response['Reservations']:
        for instance in reservation['Instances']:
            launch_time = instance['LaunchTime']
            instance_id = instance['InstanceId']
            
            # Identify instances running longer than 1 hour (likely hung/zombie)
            if now - launch_time > max_lifespan:
                print(f"Identifying zombie instance {instance_id} launched at {launch_time}")
                instances_to_terminate.append(instance_id)
                
    if instances_to_terminate:
        print(f"Terminating {len(instances_to_terminate)} instances...")
        ec2.terminate_instances(InstanceIds=instances_to_terminate)
    else:
        print("No zombie instances found.")
```

### 3. Spot Instance Reclamation
Using EC2 Spot instances for self-hosted runners reduces computing bills by up to 90%. However, AWS can reclaim the instance with a 2-minute warning.
* **Mitigation:** Configure a local script on the runner to query the Metadata Service (`http://169.254.169.254/latest/meta-data/spot/termination-time`) every 5 seconds. If a reclamation warning is issued, the script triggers a clean-up handler to gracefully deregister the runner from GitHub Actions (`./config.sh remove`), allowing the GitHub orchestrator to reschedule the interrupted job onto a fresh runner.

Implementing ephemeral, self-hosted runner orchestration with OIDC authentication requires upfront architectural complexity. However, by eliminating persistent state, container socket sharing, and long-lived static secrets, you successfully move your build pipeline security model from trust-by-default to verified zero-trust.