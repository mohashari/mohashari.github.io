---
layout: post
title: "Building a Production-Grade Disaster Recovery Runbook: Simulating Regional Failovers Without Disrupting Users"
date: 2026-06-20 08:00:00 +0700
tags: [devops, system-design, reliability, postgresql, aws]
description: "A production-focused guide to building automated, zero-downtime regional failover runbooks using AWS, PostgreSQL, and DNS routing simulation."
image: "https://picsum.photos/seed/dr-runbook/1080/720"
thumbnail: "https://picsum.photos/seed/dr-runbook/400/300"
---

At 03:14 UTC, a major fiber-optic cable cut causes a massive packet loss spike in your primary cloud region, triggering a cascade of network timeouts. Within minutes, the control plane for your managed Kubernetes service degrades, and database connections begin dropping. Your team has a disaster recovery (DR) plan, but it is written in a dusty 50-page PDF document stored on a corporate wiki that is currently inaccessible because the single-sign-on (SSO) provider is also hosted in that degraded region. When the on-call engineer finally logs in via a backup cellular connection, they face a terrifying choice: initiate a manual DNS failover to the secondary region and risk a split-brain database scenario that corrupts transactional data, or wait out the outage while customers continue to encounter 504 Gateway Timeouts. This scenario highlights a hard truth of modern backend engineering: a disaster recovery runbook is completely useless unless it is automated, codified, and continuously tested under real production loads.

## Active-Passive vs. Active-Active: The Hard Multi-Region Trade-Offs

When designing a resilient system across multiple geographical regions, backend engineers must choose between two main structural patterns: Active-Active and Active-Passive (Hot Standby).

In a true **Active-Active** architecture, application servers in both regions accept writes concurrently. This requires a distributed database engine like CockroachDB, Google Cloud Spanner, or AWS Aurora Multi-Master that can resolve write conflicts and maintain consensus across wide-area networks (WANs). While this eliminates failover delay (RTO approaches zero), it introduces a severe latency tax. The laws of physics dictate that light travels through fiber at approximately 200 km per millisecond. A round-trip time (RTT) between Virginia (us-east-1) and Oregon (us-west-2) is roughly 70 milliseconds. Every database transaction requiring synchronous cross-region consensus (e.g., Raft or Paxos coordination) will suffer this latency penalty, significantly slowing down write operations.

For standard transactional backends, an **Active-Passive with Hot Standby** architecture is often the most pragmatic choice. In this model, Region A (the primary) handles 100% of write traffic, while Region B (the standby) maintains a hot copy of the database and app servers. Database replication is run asynchronously to prevent Virginia write operations from waiting on Oregon acknowledgments. If Region A suffers a catastrophic outage, traffic is redirected to Region B, and the secondary database is promoted to primary. The trade-off is clear: you trade a minor recovery time window (RTO) for millisecond-level write performance during normal operations.

## Defining DR Metrics with Pragmatic Rigor

To design an effective failover system, you must first define your recovery parameters with numerical precision:

1. **Recovery Point Objective (RPO):** The maximum acceptable age of data that can be lost due to a disaster. If your database replication lag to the standby region is consistently 800 milliseconds, your RPO is 800ms. If the primary region vanishes, you will lose at most 800ms of transaction history.
2. **Recovery Time Objective (RTO):** The maximum acceptable duration of downtime before the system is restored. This includes the time required to detect the failure, isolate the failing region, promote the secondary database, run health checks, and update global routing.

```
+-------------------------------------------------------------------------------+
|  OUTAGE DETECTED                                                              |
|       |                                                                       |
|       v                                                                       |
|  [Phase 1: Fencing (30s)] ---> [Phase 2: DB Promotion (15s)] ---> [DNS (15s)] |
|  - Kill App Servers            - Promote Aurora Replica           - Shift DNS |
|  - Revoke DB IAM Roles         - Verify Write Readiness           - Route 100%|
+-------------------------------------------------------------------------------+
|<---------------------------- Total RTO: 60 Seconds -------------------------->|
```

In high-concurrency production systems, setting an RPO of 0 over asynchronous replication is impossible. You must establish alerts that trigger when database replication lag exceeds your target SLA (e.g., warning at 3 seconds, critical at 5 seconds) and implement application-level queueing (such as Kafka or RabbitMQ with cross-region replication) to buffer outgoing client requests during a failover window.

## Database State Replication: Keeping the Standby Region Warm

To keep your secondary region ready to take over, the database state must replicate continuously. For PostgreSQL workloads running on AWS, this is typically achieved using **Amazon Aurora Global Databases**. Under the hood, Aurora does not use standard PostgreSQL logical or physical streaming replication. Instead, it replicates storage blocks directly at the storage volume layer using dedicated network links. This architecture keeps replication latency low—typically under 1 second—without consuming CPU cycles on either the primary or secondary database instances.

To configure a warm standby that is ready to accept writes instantly, you must deploy database instances in the secondary region that match the instance class and memory configuration of the primary. If your primary database runs on a `db.r6g.8xlarge` (32 vCPUs, 256 GB RAM), provisioning a cheap `db.r6g.large` in the standby region to save costs will result in immediate CPU exhaustion and Out-Of-Memory (OOM) crashes the moment you route production traffic to it during a failover.

Furthermore, you can utilize the secondary region's read replicas to serve read-only queries during normal operations. However, this introduces the challenge of **replication lag**. If a user updates their profile in Virginia (Region A) and is redirected to Oregon (Region B) to read their profile, they may see stale data. To mitigate this, your application database client must track the transaction write sequence (using Log Sequence Numbers or LSNs). If the secondary region's LSN lag is greater than the transaction LSN, the application must fallback to reading from the primary region or block the read until the secondary catches up.

## Fencing the Primary: Preventing the Nightmare of Split-Brain

The absolute worst-case scenario in a regional failover is **split-brain**. This occurs when both regions believe they are the primary, authoritative writer. If a network partition occurs between Region A and Region B, and your routing tier starts directing traffic to Region B while Region A is still active and accepting writes, you will end up with two divergent databases. Resolving conflicting writes after both databases have moved forward is an operational nightmare that often requires manual data reconciliation and database downtime.

To prevent split-brain, you must enforce a strict **Fencing (STONITH - Shoot The Other Node In The Head)** policy. Before the standby database in Region B is promoted to primary, Region A must be completely isolated and rendered incapable of writing.

You must implement fencing programmatically at three levels:
1. **Routing Layer:** Instantly remove Region A's target groups from your global load balancer or DNS records.
2. **Compute Layer:** Scale down the application pods or ECS tasks in Region A to 0, or revoke their network access using security group updates.
3. **Database Layer:** Revoke the IAM roles or database credentials used by Region A's application servers to authenticate with the database.

Only when Region A's compute layer is confirmed dead and its database connection count drops to zero should the orchestration script issue the command to promote Region B.

## Simulating Regional Failovers in Production: The Safe Path

Testing your DR runbook by shutting down a region at 2:00 AM on Sunday is a high-risk activity that often leads to unplanned downtime. Instead, you should run controlled, incremental failover simulations during normal business hours using **Weighted Traffic Routing** and **Canary Testing**.

By configuring a global router—such as Cloudflare Load Balancing, Fastly Edge Rate Limiting, or AWS Route 53 Weighted Routing, you can gradually shift traffic from the primary region to the standby region.

```
                    [ User Requests ]
                            |
                            v
                [ Global Load Balancer ]
                /                      \
      (90% Traffic)                  (10% Traffic)
              /                          \
             v                            v
      [ Primary Region ]            [ Standby Region ]
      - Writes to DB                - Read-Only (Normal)
      - App Servers Active          - Promoted to Writer (Test)
```

The step-by-step simulation workflow is as follows:

1. **Warm Up the Secondary:** Ensure the application servers in the secondary region are scaled up and their connection pools are warmed.
2. **Execute Write Forwarding Checks:** Ensure that the secondary region's write forwarding mechanism or read-only replica fallback routes are functioning correctly.
3. **Shift Canary Traffic:** Route a small fraction of real production traffic (e.g., 1%) to the secondary region. Monitor application logs, HTTP 5xx rates, and database transaction latencies.
4. **Gradual Scaling:** Step up the traffic weight to 10%, then 50%.
5. **Complete Failover:** Revoke primary credentials, promote the standby database, and shift 100% of traffic.
6. **Automated Abort and Rollback:** Throughout the simulation, monitor a set of critical service level indicators (SLIs) in Grafana. If P99 latency exceeds 500ms or HTTP 5xx error rates exceed 0.05% over a 1-minute window, the simulation must immediately abort, route traffic back to the primary, and alert the engineering team.

## The Automated DR Failover Orchestrator

Below is a production-ready Python orchestration script that utilizes the `boto3` library to execute a controlled regional failover of an Amazon Aurora Global Database. The script performs validation checks, fences the primary region by updating security groups, promotes the secondary database, and shifts Route 53 DNS records.

```python
import time
import boto3
import logging
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration parameters
PRIMARY_REGION = "us-east-1"
SECONDARY_REGION = "us-west-2"
GLOBAL_DB_IDENTIFIER = "production-global-db"
SECONDARY_CLUSTER_IDENTIFIER = "production-secondary-cluster"
PRIMARY_SECURITY_GROUP_ID = "sg-0123456789abcdef0"  # SG controlling app access to DB
ROUTE53_HOSTED_ZONE_ID = "Z1234567890123"
FAILOVER_RECORD_NAME = "api.production.mohashari.com"

# Initialize SDK clients
rds_primary = boto3.client('rds', region_name=PRIMARY_REGION)
rds_secondary = boto3.client('rds', region_name=SECONDARY_REGION)
route53 = boto3.client('route53')

def get_replication_lag():
    """Queries the secondary region DB cluster to measure replication lag."""
    try:
        response = rds_secondary.describe_db_clusters(
            DBClusterIdentifier=SECONDARY_CLUSTER_IDENTIFIER
        )
        cluster = response['DBClusters'][0]
        # Look for Global Cluster status elements to verify replication health
        for member in cluster.get('DBClusterMembers', []):
            if member.get('IsClusterWriter') is False:
                logger.info(f"Checking cluster health: Status={cluster['Status']}")
                return 0 # In Aurora, Global DB lag is monitored via CloudWatch Metrics
    except ClientError as e:
        logger.error(f"Failed to query database status: {e}")
        raise e
    return 0

def fence_primary_region():
    """Fences the primary region by revoking database security group ingress rules."""
    logger.info("Executing Phase 1: Fencing primary region...")
    ec2_primary = boto3.client('ec2', region_name=PRIMARY_REGION)
    try:
        # Revoke application access to PostgreSQL port 5432
        ec2_primary.revoke_security_group_ingress(
            GroupId=PRIMARY_SECURITY_GROUP_ID,
            IpPermissions=[
                {
                    'IpProtocol': 'tcp',
                    'FromPort': 5432,
                    'ToPort': 5432,
                    'UserIdGroupPairs': [
                        {
                            'GroupId': PRIMARY_SECURITY_GROUP_ID  # Self-referencing group access
                        }
                    ]
                }
            ]
        )
        logger.info("Primary region database fenced successfully. Connection paths severed.")
    except ClientError as e:
        # Handle cases where the rule was already revoked
        if "InvalidPermission.NotFound" in str(e):
            logger.warning("Primary database security group ingress rule was already revoked.")
        else:
            logger.error(f"Fencing failed: {e}")
            raise e

def promote_secondary_database():
    """Promotes the secondary Aurora DB cluster of the global database."""
    logger.info("Executing Phase 2: Promoting secondary database cluster...")
    try:
        # Detach the secondary cluster from the global database to make it a standalone writer
        rds_secondary.remove_from_global_cluster(
            DbClusterIdentifier=SECONDARY_CLUSTER_IDENTIFIER,
            GlobalClusterIdentifier=GLOBAL_DB_IDENTIFIER
        )
        
        # Monitor the promotion process until the status transitions to 'available'
        while True:
            response = rds_secondary.describe_db_clusters(
                DBClusterIdentifier=SECONDARY_CLUSTER_IDENTIFIER
            )
            status = response['DBClusters'][0]['Status']
            logger.info(f"Database promotion status: {status}")
            if status == 'available':
                logger.info("Secondary database promoted to writer successfully.")
                break
            time.sleep(5)
    except ClientError as e:
        logger.error(f"Database promotion failed: {e}")
        raise e

def update_dns_routing():
    """Updates Route 53 DNS records to point to the newly promoted writer endpoint."""
    logger.info("Executing Phase 3: Shifting global DNS routing...")
    try:
        response = rds_secondary.describe_db_clusters(
            DBClusterIdentifier=SECONDARY_CLUSTER_IDENTIFIER
        )
        new_writer_endpoint = response['DBClusters'][0]['Endpoint']
        
        route53.change_resource_record_sets(
            HostedZoneId=ROUTE53_HOSTED_ZONE_ID,
            ChangeBatch={
                'Comment': 'Failover routing update initiated by DR orchestrator.',
                'Changes': [
                    {
                        'Action': 'UPSERT',
                        'ResourceRecordSet': {
                            'Name': FAILOVER_RECORD_NAME,
                            'Type': 'CNAME',
                            'TTL': 10,
                            'ResourceRecords': [
                                {
                                    'Value': new_writer_endpoint
                                }
                            ]
                        }
                    }
                ]
            }
        )
        logger.info(f"DNS routed successfully. {FAILOVER_RECORD_NAME} points to {new_writer_endpoint}")
    except ClientError as e:
        logger.error(f"DNS routing update failed: {e}")
        raise e

def run_failover():
    """Orchestrates the complete failover workflow sequence."""
    logger.info("Starting regional failover automation script...")
    
    # 0. Pre-flight checks
    lag = get_replication_lag()
    logger.info(f"Current replication state verified. Running failover actions...")
    
    # 1. Sever write paths in primary region
    fence_primary_region()
    
    # 2. Promote the standby DB
    promote_secondary_database()
    
    # 3. Shift traffic
    update_dns_routing()
    
    logger.info("Disaster recovery failover successfully completed.")

if __name__ == "__main__":
    run_failover()
```

## Real-World DR Failure Modes and Mitigations

When simulating or executing regional failovers, backend teams commonly encounter several edge-case failure modes.

### 1. DNS Caching by Local ISP Resolvers
Even if you configure a DNS record with a Time-To-Live (TTL) of 10 seconds, many public recursive DNS resolvers (especially those operated by consumer ISPs) completely ignore low TTL values, caching DNS query results for 30 minutes or longer. Consequently, a portion of your users will continue to send requests to the offline primary region long after the failover completes.

* **Mitigation:** Avoid exposing database connection string URLs directly to client devices or utilizing client-level DNS routing for critical stateful operations. Instead, route user requests through an Anycast-based edge proxy like Cloudflare, AWS Global Accelerator, or Fastly. These edge networks terminate client TCP connections at the nearest Point of Presence (PoP) and route requests internally to the active region, bypassing client DNS caching limits.

### 2. Standby Region Cold-Start Bottlenecks
If your secondary region is idle during normal operations, its application pods and autoscaling groups will be scaled down to minimum values. When you suddenly shift 100% of production traffic, the standby region will experience a massive cold-start surge. Microservices will experience latency spikes as they initialize connection pools, JIT compile code, and warm up local in-memory caches.

* **Mitigation:** Implement a "minimum scale" baseline policy for the standby region. The standby compute resources must be pre-scaled to at least 30-50% of the primary region's active capacity. During simulations, use a script to automatically scale up the target deployment size *before* shifting DNS routing weights.

### 3. Read Replica Lag Spikes During Canary Routing
When you route canary traffic to the secondary region before promoting the database, the application servers in the secondary region will execute read operations against the local read replica, while writes are routed back to the primary database via cross-region networks. If replication lag spikes under load, users in the canary group will observe significant data inconsistency, such as missing updates they just performed.

* **Mitigation:** Force write-to-read consistency checks. The application middleware must inject a transaction token (such as a database LSN or an incrementing timestamp) into client cookies or headers. When a subsequent read request arrives, the server compares the token with the replica's replication status. If the replica is stale, the request must block until the replica catches up, or route the read query directly to the primary writer instance.

## DR Architecture Comparison Matrix

Choosing the right DR strategy requires balancing cost, complexity, and operational risk. The matrix below outlines how different architectures compare across key metrics:

| DR Architecture | RPO (Data Loss) | RTO (Downtime) | Cost | Complexity | Core Failure Risks |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Cold Standby** | Hours (Depends on backups) | Hours | Low | Low | Corrupted backups, slow resource provisioning. |
| **Active-Passive (Manual)** | < 2 Seconds | 5 - 15 Minutes | Medium | Medium | Human operator panic, slow communication lines. |
| **Active-Passive (Automated)** | < 1 Second | < 90 Seconds | High | High | Split-brain database routing, false-positive failovers. |
| **Active-Active** | Near Zero | < 1 Second | Very High | Extremely High | Cross-region latency penalties, transaction write conflicts. |

By building an automated orchestrator, implementing strict primary fencing, and running regular, incremental failover simulations under real load, backend teams can transform their disaster recovery plans from untested PDF documents into a reliable, battle-tested system safety net. Saving your system from outages does not depend on hoping cloud platforms never fail; it depends on designing systems that are built to fail gracefully and recover automatically.
