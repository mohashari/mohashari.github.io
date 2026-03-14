---
layout: post
title: "Kubernetes for Backend Engineers: From Zero to Deployed"
tags: [kubernetes, devops, backend]
description: "A practical Kubernetes guide for backend engineers who want to deploy, scale, and manage their services like a pro."
---

Kubernetes (K8s) can feel overwhelming at first. Pods, Services, Deployments, Ingress — it's a lot. But once you understand the mental model, everything clicks. This guide cuts through the noise and gives you what you actually need as a backend engineer.

![Kubernetes Cluster Architecture](/images/diagrams/kubernetes-architecture.svg)

## The Core Mental Model

Think of Kubernetes as a **desired state machine**. You tell it what you want (e.g., "run 3 replicas of my API"), and Kubernetes constantly works to make that reality. It's declarative, not imperative.

```
You declare desired state → Kubernetes reconciles → Actual state matches desired state
```

## Key Primitives

### Pod

The smallest deployable unit. A pod wraps one (or more) containers that share network and storage.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: my-api
spec:
  containers:
  - name: api
    image: myapp:1.0.0
    ports:
    - containerPort: 8080
```

You almost never create bare Pods — use Deployments instead.

### Deployment

Manages a ReplicaSet which manages Pods. This is how you run your application.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-deployment
spec:
  replicas: 3
  selector:
    matchLabels:
      app: my-api
  template:
    metadata:
      labels:
        app: my-api
    spec:
      containers:
      - name: api
        image: myapp:1.0.0
        resources:
          requests:
            memory: "128Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        readinessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 10
```

### Service

Services give your pods a stable network identity. Pods die and restart with new IPs — Services provide a stable endpoint.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: api-service
spec:
  selector:
    app: my-api
  ports:
  - port: 80
    targetPort: 8080
  type: ClusterIP
```

### ConfigMap and Secret

Decouple configuration from your container image:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: api-config
data:
  LOG_LEVEL: "info"
  MAX_CONNECTIONS: "100"

---

apiVersion: v1
kind: Secret
metadata:
  name: api-secrets
type: Opaque
data:
  # base64 encoded values
  DB_PASSWORD: cGFzc3dvcmQxMjM=
```

Reference them in your deployment:

```yaml
env:
- name: LOG_LEVEL
  valueFrom:
    configMapKeyRef:
      name: api-config
      key: LOG_LEVEL
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: api-secrets
      key: DB_PASSWORD
```

## Rolling Updates with Zero Downtime

By default, Kubernetes does rolling updates — replacing old pods one by one:

```yaml
spec:
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1        # Can have 1 extra pod during update
      maxUnavailable: 0  # Never have fewer than desired count
```

Combined with readiness probes, this gives you true zero-downtime deployments.

## Horizontal Pod Autoscaler

Scale based on CPU/memory automatically:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: api-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: api-deployment
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
```

## Essential kubectl Commands

```bash
# See what's running
kubectl get pods -n my-namespace
kubectl get deployments

# Debug a pod
kubectl describe pod <pod-name>
kubectl logs <pod-name> --tail=100 -f

# Execute into a container
kubectl exec -it <pod-name> -- /bin/sh

# Apply a manifest
kubectl apply -f deployment.yaml

# Rollback a deployment
kubectl rollout undo deployment/api-deployment

# Check rollout status
kubectl rollout status deployment/api-deployment
```

## Takeaway

Start simple: Deployment → Service → ConfigMap/Secret. Add Ingress when you need HTTP routing. Add HPA when you need autoscaling. Build up complexity only as needed.
