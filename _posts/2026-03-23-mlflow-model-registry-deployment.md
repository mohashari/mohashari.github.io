---
layout: post
title: "MLflow Model Registry: From Experiment to Production Deployment"
date: 2026-03-23 08:00:00 +0700
tags: [mlflow, mlops, model-serving, ci-cd, ai-engineering]
description: "How to use MLflow Model Registry to enforce reproducibility, staged promotion, and safe rollbacks in production ML pipelines."
---

Every ML team eventually hits the same wall: a data scientist trains a model that beats the baseline by 4%, drops a `.pkl` file in a shared S3 bucket, Slacks the platform team a path, and calls it a handoff. Six weeks later, production is serving a model nobody can reproduce, the training code has diverged from what generated it, and rollback means restoring from a backup nobody labeled. MLflow Model Registry exists to prevent exactly this. It's not glamorous infrastructure, but it's the difference between a team that ships models confidently and one that treats every deployment like defusing a bomb.

## What the Registry Actually Does

The Registry is a centralized store for versioned model artifacts with a lifecycle state machine: `None → Staging → Production → Archived`. Each registered model version carries a pointer to the underlying MLflow run, meaning you get the full lineage: training code commit hash, hyperparameters, dataset hash, and evaluation metrics — all queryable. The artifact itself lives in your configured artifact store (S3, GCS, Azure Blob, or local), but the Registry metadata lives in the MLflow tracking server's backing database (Postgres in any serious setup).

The lifecycle transitions are the key. Instead of overwriting a model in place, you register a new version, promote it through stages via API or UI, and the old version stays archived and deployable. You can query "what model version is currently in Production" programmatically, which is what makes CI/CD integration tractable.

## Registering a Model From a Training Run

The simplest entry point is logging a model during training and registering it in one shot:

```python
# snippet-1
import mlflow
import mlflow.sklearn
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import roc_auc_score
import numpy as np

MLFLOW_TRACKING_URI = "http://mlflow.internal:5000"
MODEL_NAME = "fraud-detector"
PROMOTION_AUC_THRESHOLD = 0.92

mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
mlflow.set_experiment("fraud-detection-v3")

with mlflow.start_run() as run:
    model = GradientBoostingClassifier(
        n_estimators=500,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.8,
        random_state=42,
    )
    model.fit(X_train, y_train)

    y_proba = model.predict_proba(X_val)[:, 1]
    auc = roc_auc_score(y_val, y_proba)

    mlflow.log_params(model.get_params())
    mlflow.log_metric("val_auc", auc)
    mlflow.log_metric("val_size", len(y_val))

    # Register immediately — creates a new version under MODEL_NAME
    model_uri = f"runs:/{run.info.run_id}/model"
    mv = mlflow.register_model(model_uri, MODEL_NAME)

    print(f"Registered {MODEL_NAME} version {mv.version}, run {run.info.run_id}")
```

`mlflow.register_model` is synchronous by default but model registration is async under the hood — the version enters `PENDING_REGISTRATION` state briefly before becoming `READY`. If you're scripting this in CI and immediately querying the version, wait for `READY`:

```python
# snippet-2
import time
from mlflow.tracking import MlflowClient

client = MlflowClient(tracking_uri=MLFLOW_TRACKING_URI)

def wait_until_ready(model_name: str, version: str, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        mv = client.get_model_version(model_name, version)
        if mv.status == "READY":
            return
        if mv.status == "FAILED_REGISTRATION":
            raise RuntimeError(f"Model registration failed: {mv.status_message}")
        time.sleep(2)
    raise TimeoutError(f"Model version {version} did not become READY in {timeout}s")

wait_until_ready(MODEL_NAME, mv.version)
```

## Automating Promotion Based on Evaluation Thresholds

The real leverage is in automating the `None → Staging → Production` transitions. The naive approach is to promote anything that finishes training. The production approach is to gate promotion on evaluation metrics, run a champion/challenger comparison, and only promote when the new version strictly beats the current production version.

```python
# snippet-3
from mlflow.entities.model_registry import ModelVersion
from typing import Optional

def get_production_version(client: MlflowClient, model_name: str) -> Optional[ModelVersion]:
    versions = client.get_latest_versions(model_name, stages=["Production"])
    return versions[0] if versions else None

def promote_to_staging_if_qualifies(
    client: MlflowClient,
    model_name: str,
    candidate_version: str,
    min_auc: float = PROMOTION_AUC_THRESHOLD,
) -> bool:
    mv = client.get_model_version(model_name, candidate_version)
    run = client.get_run(mv.run_id)
    candidate_auc = run.data.metrics.get("val_auc", 0.0)

    if candidate_auc < min_auc:
        print(f"Version {candidate_version} AUC {candidate_auc:.4f} below threshold {min_auc}. Skipping.")
        client.set_model_version_tag(model_name, candidate_version, "promotion_blocked", "auc_below_threshold")
        return False

    prod_version = get_production_version(client, model_name)
    if prod_version:
        prod_run = client.get_run(prod_version.run_id)
        prod_auc = prod_run.data.metrics.get("val_auc", 0.0)
        if candidate_auc <= prod_auc:
            print(f"Candidate AUC {candidate_auc:.4f} does not beat production {prod_auc:.4f}. Skipping.")
            client.set_model_version_tag(model_name, candidate_version, "promotion_blocked", "no_improvement")
            return False

    client.transition_model_version_stage(
        name=model_name,
        version=candidate_version,
        stage="Staging",
        archive_existing_versions=False,  # keep old staging versions for audit
    )
    client.set_model_version_tag(model_name, candidate_version, "promoted_by", "ci-pipeline")
    print(f"Version {candidate_version} promoted to Staging (AUC: {candidate_auc:.4f})")
    return True
```

The `archive_existing_versions=False` is deliberate. Archiving is a destructive state change — you lose the ability to quickly roll back to a previous staging version without going through the full transition dance again. Keep explicit control.

## Integrating Into a CI/CD Pipeline

The full flow from training trigger to Staging promotion fits naturally into a GitHub Actions workflow or a GitLab CI job. The pattern: train → evaluate → register → gate → promote.

```yaml
# snippet-4
# .github/workflows/model-promotion.yml
name: Model Training and Promotion

on:
  push:
    paths:
      - 'training/**'
      - 'features/**'

jobs:
  train-and-promote:
    runs-on: self-hosted
    environment: ml-staging
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements-train.txt

      - name: Train model
        env:
          MLFLOW_TRACKING_URI: ${{ secrets.MLFLOW_TRACKING_URI }}
          MLFLOW_TRACKING_TOKEN: ${{ secrets.MLFLOW_TRACKING_TOKEN }}
          AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
        run: |
          python training/train.py --output-version-file /tmp/model_version.txt

      - name: Promote to Staging
        env:
          MLFLOW_TRACKING_URI: ${{ secrets.MLFLOW_TRACKING_URI }}
          MLFLOW_TRACKING_TOKEN: ${{ secrets.MLFLOW_TRACKING_TOKEN }}
        run: |
          VERSION=$(cat /tmp/model_version.txt)
          python scripts/promote_to_staging.py --version "$VERSION"

      - name: Run integration tests against Staging model
        run: |
          VERSION=$(cat /tmp/model_version.txt)
          python tests/integration/test_model_serving.py --version "$VERSION" --stage Staging
```

The training script writes the registered version number to a file, which threads through subsequent steps. This avoids querying "latest version" by timestamp, which is racy when multiple training jobs run in parallel.

## Production Promotion and the Champion/Challenger Gate

Staging → Production is where governance matters most. This transition should require either a manual approval (via the MLflow UI webhook or a PR approval gate) or a shadow traffic evaluation. For teams running online evaluation, the pattern is:

1. Deploy Staging model behind a feature flag serving 10% of traffic.
2. Collect production metrics for 48 hours (precision/recall on labeled outcomes, latency p99).
3. Run promotion script that reads production metrics from your observability store and compares against incumbent.

```python
# snippet-5
import requests
from datetime import datetime, timedelta

PROMETHEUS_URL = "http://prometheus.internal:9090"

def query_production_auc(model_version: str, window_hours: int = 48) -> float:
    """Query Prometheus for online AUC metric collected by the serving layer."""
    end = datetime.utcnow()
    start = end - timedelta(hours=window_hours)
    query = (
        f'avg_over_time(model_online_auc{{version="{model_version}",'
        f'stage="staging"}}[{window_hours}h])'
    )
    resp = requests.get(
        f"{PROMETHEUS_URL}/api/v1/query",
        params={"query": query, "time": end.timestamp()},
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    if not result:
        raise ValueError(f"No online AUC data for version {model_version}")
    return float(result[0]["value"][1])

def promote_to_production(
    client: MlflowClient,
    model_name: str,
    candidate_version: str,
    min_online_auc: float = 0.90,
) -> None:
    online_auc = query_production_auc(candidate_version)
    if online_auc < min_online_auc:
        raise ValueError(
            f"Online AUC {online_auc:.4f} below production threshold {min_online_auc}. "
            f"Blocking promotion of version {candidate_version}."
        )

    # Archive current production versions atomically
    client.transition_model_version_stage(
        name=model_name,
        version=candidate_version,
        stage="Production",
        archive_existing_versions=True,  # safe here — we want clean production state
    )
    client.set_model_version_tag(model_name, candidate_version, "promoted_at", datetime.utcnow().isoformat())
    client.set_model_version_tag(model_name, candidate_version, "online_auc_at_promotion", str(online_auc))
    print(f"Version {candidate_version} is now Production (online AUC: {online_auc:.4f})")
```

Tag the promotion metrics onto the model version. When you're debugging a degraded model six months later, you want to know what the online AUC was at promotion time without reconstructing it from Prometheus.

## Loading Models in the Serving Layer

The serving layer shouldn't hardcode model paths. It should query the Registry for the current Production version at startup (and optionally on a poll interval for hot-swapping):

```python
# snippet-6
import mlflow.pyfunc
from mlflow.tracking import MlflowClient
import threading
import time
import logging

logger = logging.getLogger(__name__)

class RegistryBackedModelServer:
    def __init__(self, model_name: str, tracking_uri: str, reload_interval: int = 300):
        self.model_name = model_name
        self.client = MlflowClient(tracking_uri=tracking_uri)
        self.reload_interval = reload_interval
        self._model = None
        self._current_version = None
        self._lock = threading.RLock()
        self._load_production_model()
        self._start_reload_thread()

    def _load_production_model(self) -> None:
        versions = self.client.get_latest_versions(self.model_name, stages=["Production"])
        if not versions:
            raise RuntimeError(f"No Production version found for model '{self.model_name}'")
        v = versions[0]
        if v.version == self._current_version:
            return  # no change
        logger.info(f"Loading {self.model_name} version {v.version} from {v.source}")
        new_model = mlflow.pyfunc.load_model(f"models:/{self.model_name}/Production")
        with self._lock:
            self._model = new_model
            self._current_version = v.version
        logger.info(f"Now serving {self.model_name} version {v.version}")

    def _start_reload_thread(self) -> None:
        def poll():
            while True:
                time.sleep(self.reload_interval)
                try:
                    self._load_production_model()
                except Exception as e:
                    logger.error(f"Model reload failed: {e}")  # don't crash the server
        t = threading.Thread(target=poll, daemon=True)
        t.start()

    def predict(self, data):
        with self._lock:
            return self._model.predict(data)
```

The reload thread means a Production promotion automatically propagates to serving within `reload_interval` seconds without a service restart. The `RLock` prevents serving a half-loaded model during the swap.

## Rollback Is a First-Class Operation

When production metrics degrade — and they will — rollback should be a one-liner. Since old versions are archived rather than deleted, this is just a state transition:

```bash
# snippet-7
#!/bin/bash
# scripts/rollback.sh — rolls back to the most recent Archived version

set -euo pipefail

MODEL_NAME="${1:?Usage: rollback.sh <model_name> [version]}"
TARGET_VERSION="${2:-}"  # optional: specify exact version, otherwise pick most recent archived

TRACKING_URI="${MLFLOW_TRACKING_URI:?MLFLOW_TRACKING_URI not set}"

if [ -z "$TARGET_VERSION" ]; then
  TARGET_VERSION=$(python3 - <<EOF
from mlflow.tracking import MlflowClient
client = MlflowClient(tracking_uri="$TRACKING_URI")
archived = client.get_latest_versions("$MODEL_NAME", stages=["Archived"])
if not archived:
    raise SystemExit("No archived versions to roll back to")
# pick most recently transitioned
latest = sorted(archived, key=lambda v: v.last_updated_timestamp, reverse=True)[0]
print(latest.version)
EOF
  )
fi

echo "Rolling back $MODEL_NAME to version $TARGET_VERSION"

python3 - <<EOF
from mlflow.tracking import MlflowClient
from datetime import datetime

client = MlflowClient(tracking_uri="$TRACKING_URI")
client.transition_model_version_stage(
    name="$MODEL_NAME",
    version="$TARGET_VERSION",
    stage="Production",
    archive_existing_versions=True,
)
client.set_model_version_tag("$MODEL_NAME", "$TARGET_VERSION", "rollback_at", datetime.utcnow().isoformat())
print(f"Version $TARGET_VERSION is now Production")
EOF
```

The serving layer's reload thread picks this up automatically within the configured interval. No restarts, no redeployments. The tag records the rollback event directly on the model version for audit purposes.

## Failure Modes Worth Knowing

**Stale `get_latest_versions` caching.** The Python client caches Registry responses for a short window. In fast-moving pipelines, call `client.get_model_version(name, version)` with explicit version numbers rather than relying on `get_latest_versions` when exact state matters.

**Artifact store permissions diverging from tracking server permissions.** The Registry metadata is in Postgres; the artifacts are in S3. A team can have read access to the Registry but no access to the underlying S3 prefix. Model loading will fail at runtime with a cryptic boto3 error. Audit both IAM policies, not just MLflow RBAC.

**Concurrent promotions from parallel training runs.** If two CI jobs train models from the same commit and both qualify for promotion, both will try to archive the existing Production version and set themselves as Production. The last write wins, and you get undefined state. Serialize promotion jobs with a distributed lock (Redis `SET NX` or a database advisory lock) around the Staging → Production transition.

**Missing run metadata for old versions.** MLflow runs are separate from Registry versions. If you clean up old runs (common to manage storage costs), you lose the lineage for any versions registered against those runs. Set retention policies on runs independently from retention policies on model versions, and never delete runs for versions that are in Staging or Production.

The Registry doesn't solve the hard problems of ML in production — data drift, training/serving skew, label delay. What it does is remove the accidental complexity: the shared S3 bucket of unlabeled pickle files, the tribal knowledge about which model is deployed where, the three-day rollback that should have taken three minutes. That's enough to be worth the operational overhead.
```