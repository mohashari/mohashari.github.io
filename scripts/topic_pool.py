import json
import random
import datetime
from pathlib import Path

CATEGORIES = {
    "software_engineering": [
        ("hexagonal-architecture-ports-adapters", "Hexagonal Architecture: Ports & Adapters in Production"),
        ("event-sourcing-cqrs-deep-dive", "Event Sourcing and CQRS: A Production Deep Dive"),
        ("distributed-consensus-raft-paxos", "Distributed Consensus: Raft vs Paxos Explained"),
        ("zero-downtime-database-migrations", "Zero-Downtime Database Migrations at Scale"),
        ("domain-driven-design-aggregates", "Domain-Driven Design: Aggregates That Actually Work"),
        ("saga-pattern-distributed-transactions", "The Saga Pattern for Distributed Transactions"),
        ("circuit-breaker-resilience-patterns", "Circuit Breakers and Resilience Patterns in Microservices"),
        ("api-versioning-strategies", "API Versioning Strategies: Breaking Changes Without Breaking Clients"),
        ("idempotency-distributed-systems", "Idempotency in Distributed Systems: Design and Implementation"),
        ("caching-strategies-cache-invalidation", "Caching Strategies and the Art of Cache Invalidation"),
        ("message-queue-patterns-kafka", "Message Queue Patterns: Beyond Basic Pub/Sub with Kafka"),
        ("rate-limiting-algorithms-production", "Rate Limiting Algorithms: Token Bucket, Leaky Bucket, Sliding Window"),
        ("consistent-hashing-load-balancing", "Consistent Hashing: Load Balancing Without Rehashing"),
        ("backpressure-flow-control-systems", "Backpressure and Flow Control in Distributed Systems"),
        ("two-phase-commit-vs-saga", "Two-Phase Commit vs Saga: Choosing the Right Consistency Model"),
        ("outbox-pattern-reliable-messaging", "The Outbox Pattern: Reliable Event Publishing Without Dual Writes"),
        ("strangler-fig-migration-pattern", "Strangler Fig: Migrating Monoliths Without Big Bang Rewrites"),
        ("bulk-head-pattern-isolation", "Bulkhead Pattern: Fault Isolation in Production Services"),
        ("leader-election-distributed-systems", "Leader Election in Distributed Systems: Algorithms and Trade-offs"),
        ("write-ahead-log-storage-engines", "Write-Ahead Logging: The Foundation of Database Durability"),
        ("lsm-tree-vs-b-tree-storage", "LSM-Tree vs B-Tree: Choosing the Right Storage Engine"),
        ("bloom-filter-probabilistic-structures", "Bloom Filters and Probabilistic Data Structures in Production"),
        ("merkle-tree-data-integrity", "Merkle Trees: Data Integrity and Efficient Verification"),
        ("service-mesh-architecture-istio", "Service Mesh Architecture: Beyond the Sidecar Hype"),
        ("eventually-consistent-read-models", "Eventually Consistent Read Models: Building Reliable Projections"),
    ],
    "development": [
        ("go-error-handling-patterns", "Go Error Handling Patterns Beyond fmt.Errorf"),
        ("python-asyncio-production", "Python asyncio in Production: Pitfalls and Patterns"),
        ("docker-layer-caching-optimization", "Docker Layer Caching: Optimizing Build Times by 80%"),
        ("kubernetes-hpa-vpa-autoscaling", "Kubernetes HPA and VPA: Autoscaling That Actually Works"),
        ("postgresql-connection-pooling-pgbouncer", "PostgreSQL Connection Pooling with PgBouncer: Tuning for High Load"),
        ("redis-data-structures-advanced", "Advanced Redis Data Structures for Backend Engineers"),
        ("grpc-streaming-production", "gRPC Streaming in Production: Patterns and Pitfalls"),
        ("go-concurrency-patterns", "Go Concurrency Patterns: Channels, Context, and WaitGroups"),
        ("rest-api-design-principles", "REST API Design: Principles Senior Engineers Follow"),
        ("database-indexing-deep-dive", "Database Indexing Deep Dive: Beyond the Basics"),
        ("kafka-consumer-groups-partitions", "Kafka Consumer Groups and Partitions: Getting the Parallelism Right"),
        ("nginx-configuration-performance", "NGINX Configuration for High-Performance Backend Services"),
        ("go-testing-table-driven", "Table-Driven Tests in Go: Writing Tests That Scale"),
        ("postgresql-jsonb-performance", "PostgreSQL JSONB: When to Use It and When to Avoid It"),
        ("redis-pub-sub-vs-streams", "Redis Pub/Sub vs Streams: Choosing the Right Tool"),
        ("protobuf-schema-evolution", "Protobuf Schema Evolution: Backward and Forward Compatibility"),
        ("go-generics-practical-patterns", "Go Generics: Practical Patterns for Backend Code"),
        ("docker-multi-stage-builds", "Docker Multi-Stage Builds: Minimal Images for Production"),
        ("kubernetes-rbac-service-accounts", "Kubernetes RBAC and Service Accounts: Least Privilege in Practice"),
        ("postgresql-partitioning-strategies", "PostgreSQL Table Partitioning: Strategies for Billion-Row Tables"),
        ("go-profiling-pprof-production", "Go Profiling with pprof: Finding Performance Bottlenecks in Production"),
        ("elasticsearch-indexing-search", "Elasticsearch Indexing and Search: Production Configuration"),
        ("redis-cluster-sharding", "Redis Cluster and Sharding: Horizontal Scaling Without Data Loss"),
        ("graphql-vs-rest-tradeoffs", "GraphQL vs REST: Real Trade-offs for Backend Teams"),
        ("websocket-scaling-production", "WebSocket Scaling in Production: From Single Node to Cluster"),
    ],
    "devsecops": [
        ("sbom-supply-chain-security", "SBOM and Software Supply Chain Security: A Practical Guide"),
        ("opa-policy-as-code", "Open Policy Agent: Policy as Code for Kubernetes and APIs"),
        ("secrets-rotation-vault-k8s", "Secrets Rotation with HashiCorp Vault and Kubernetes"),
        ("container-runtime-security-falco", "Container Runtime Security with Falco: Detecting Threats in Real-Time"),
        ("sast-dast-pipeline-integration", "SAST and DAST in CI/CD Pipelines: Shifting Security Left"),
        ("kubernetes-network-policies", "Kubernetes Network Policies: Zero-Trust Networking in Practice"),
        ("trivy-container-vulnerability-scanning", "Container Vulnerability Scanning with Trivy in CI/CD"),
        ("oauth2-pkce-api-security", "OAuth2 with PKCE: Securing APIs the Right Way"),
        ("infrastructure-as-code-security-terraform", "Infrastructure as Code Security: Scanning Terraform Before Deploy"),
        ("log-aggregation-elk-security", "Log Aggregation for Security: ELK Stack in Production"),
        ("prometheus-alertmanager-slo", "SLOs with Prometheus and Alertmanager: From Toil to Reliability"),
        ("gitops-argocd-deployment", "GitOps with ArgoCD: Declarative Deployments That Actually Work"),
        ("mutual-tls-service-authentication", "Mutual TLS: Service-to-Service Authentication Without Tokens"),
        ("runtime-security-seccomp-apparmor", "Runtime Security with Seccomp and AppArmor in Kubernetes"),
        ("ci-cd-pipeline-security-github-actions", "GitHub Actions Security: Hardening Your CI/CD Pipeline"),
        ("distributed-tracing-opentelemetry", "Distributed Tracing with OpenTelemetry: End-to-End Visibility"),
        ("chaos-engineering-principles", "Chaos Engineering: Breaking Things on Purpose Before Prod Does"),
        ("kubernetes-pod-security-admission", "Kubernetes Pod Security Admission: Enforcing Standards at Scale"),
        ("image-signing-cosign-sigstore", "Container Image Signing with Cosign and Sigstore"),
        ("compliance-as-code-kubernetes", "Compliance as Code: Automating SOC2 Controls in Kubernetes"),
        ("anomaly-detection-metrics-grafana", "Anomaly Detection with Metrics: Grafana and ML-Based Alerting"),
        ("zero-trust-architecture-service-mesh", "Zero-Trust Architecture with Service Mesh: Beyond Perimeter Security"),
        ("dependency-updates-renovate-security", "Automated Dependency Updates with Renovate: Keeping Up with CVEs"),
        ("audit-logging-production-systems", "Audit Logging in Production Systems: What, When, and How"),
        ("incident-response-runbooks-automation", "Incident Response Runbooks: From Manual to Automated Remediation"),
    ],
    "ai_engineering": [
        ("rag-production-chunking-strategies", "RAG in Production: Chunking Strategies That Actually Improve Retrieval"),
        ("vector-database-comparison-pgvector-pinecone", "Vector Databases Compared: pgvector vs Pinecone vs Weaviate"),
        ("llm-observability-langfuse", "LLM Observability with Langfuse: Tracing Prompts in Production"),
        ("function-calling-tool-use-patterns", "Function Calling and Tool Use: Building Reliable AI Agents"),
        ("embeddings-fine-tuning-strategies", "Embeddings Fine-Tuning: When Generic Models Are Not Enough"),
        ("prompt-engineering-production-llms", "Prompt Engineering for Production LLMs: Techniques That Scale"),
        ("llm-evaluation-metrics-production", "LLM Evaluation in Production: Beyond Vibes-Based Testing"),
        ("semantic-caching-llm-latency", "Semantic Caching: Reducing LLM Latency and Cost by 60%"),
        ("ai-agent-orchestration-patterns", "AI Agent Orchestration: Multi-Agent Systems That Work Reliably"),
        ("context-window-management-strategies", "Context Window Management: Handling Long Documents in LLMs"),
        ("llm-gateway-rate-limiting-routing", "LLM Gateway: Rate Limiting, Routing, and Fallback for Production AI"),
        ("hybrid-search-dense-sparse-retrieval", "Hybrid Search: Combining Dense and Sparse Retrieval for Better RAG"),
        ("mlflow-model-registry-deployment", "MLflow Model Registry: From Experiment to Production Deployment"),
        ("structured-output-llm-json-schema", "Structured Output from LLMs: Reliable JSON with Schema Validation"),
        ("knowledge-graph-rag-enhancement", "Knowledge Graphs for RAG: Enhancing Retrieval with Graph Context"),
        ("llm-cost-optimization-strategies", "LLM Cost Optimization: Caching, Batching, and Model Routing"),
        ("ai-safety-guardrails-production", "AI Safety Guardrails in Production: Input/Output Validation at Scale"),
        ("fine-tuning-llm-domain-specific", "Fine-Tuning LLMs for Domain-Specific Tasks: When and How"),
        ("streaming-llm-responses-sse", "Streaming LLM Responses with SSE: Real-Time UX Without Complexity"),
        ("vector-search-filtering-metadata", "Vector Search with Metadata Filtering: Making RAG Actually Precise"),
        ("llm-prompt-injection-defense", "LLM Prompt Injection: Attack Vectors and Defense Strategies"),
        ("multi-modal-llm-vision-production", "Multi-Modal LLMs in Production: Vision, Text, and Data Pipelines"),
        ("ai-pipeline-workflow-prefect", "AI Pipelines with Prefect: Orchestrating ML Workflows at Scale"),
        ("reranking-retrieval-cohere", "Reranking in RAG: Improving Retrieval Precision with Cross-Encoders"),
        ("llm-batch-inference-optimization", "LLM Batch Inference: Throughput Optimization for Offline Pipelines"),
    ],
}

CATEGORY_NEEDS = {
    "software_engineering": {"code": True, "diagram": True},
    "development": {"code": True, "diagram": False},
    "devsecops": {"code": True, "diagram": True},
    "ai_engineering": {"code": True, "diagram": False},
}


def load_history(history_path: str) -> dict:
    p = Path(history_path)
    if not p.exists():
        return {"used": [], "last_updated": None}
    with open(p) as f:
        return json.load(f)


def save_history(history_path: str, history: dict) -> None:
    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)


def mark_used(history: dict, slugs: list) -> dict:
    history["used"] = list(set(history["used"]) | set(slugs))
    history["last_updated"] = datetime.date.today().isoformat()
    return history


def select_topics(history: dict, count: int = 20) -> list:
    used = set(history.get("used", []))
    per_category = count // len(CATEGORIES)
    selected = []

    for category, topics in CATEGORIES.items():
        available = [(slug, title) for slug, title in topics if slug not in used]
        if len(available) < per_category:
            # Reset this category's used entries
            used -= {slug for slug, _ in topics}
            available = list(topics)

        picks = random.sample(available, min(per_category, len(available)))
        needs = CATEGORY_NEEDS[category]
        for slug, title in picks:
            selected.append({
                "category": category,
                "slug": slug,
                "title": title,
                "needs_code": needs["code"],
                "needs_diagram": needs["diagram"],
            })

    random.shuffle(selected)
    return selected[:count]
