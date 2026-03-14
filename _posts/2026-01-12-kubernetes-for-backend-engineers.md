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


<script src="https://gist.github.com/mohashari/e192933c9b52b133b98523e2c93fdafb.js?file=snippet.txt"></script>


## Key Primitives

### Pod

The smallest deployable unit. A pod wraps one (or more) containers that share network and storage.


<script src="https://gist.github.com/mohashari/e192933c9b52b133b98523e2c93fdafb.js?file=snippet.yaml"></script>


You almost never create bare Pods — use Deployments instead.

### Deployment

Manages a ReplicaSet which manages Pods. This is how you run your application.


<script src="https://gist.github.com/mohashari/e192933c9b52b133b98523e2c93fdafb.js?file=snippet-2.yaml"></script>


### Service

Services give your pods a stable network identity. Pods die and restart with new IPs — Services provide a stable endpoint.


<script src="https://gist.github.com/mohashari/e192933c9b52b133b98523e2c93fdafb.js?file=snippet-3.yaml"></script>


### ConfigMap and Secret

Decouple configuration from your container image:


<script src="https://gist.github.com/mohashari/e192933c9b52b133b98523e2c93fdafb.js?file=snippet-4.yaml"></script>


Reference them in your deployment:


<script src="https://gist.github.com/mohashari/e192933c9b52b133b98523e2c93fdafb.js?file=snippet-5.yaml"></script>


## Rolling Updates with Zero Downtime

By default, Kubernetes does rolling updates — replacing old pods one by one:


<script src="https://gist.github.com/mohashari/e192933c9b52b133b98523e2c93fdafb.js?file=snippet-6.yaml"></script>


Combined with readiness probes, this gives you true zero-downtime deployments.

## Horizontal Pod Autoscaler

Scale based on CPU/memory automatically:


<script src="https://gist.github.com/mohashari/e192933c9b52b133b98523e2c93fdafb.js?file=snippet-7.yaml"></script>


## Essential kubectl Commands


<script src="https://gist.github.com/mohashari/e192933c9b52b133b98523e2c93fdafb.js?file=snippet.sh"></script>


## Takeaway

Start simple: Deployment → Service → ConfigMap/Secret. Add Ingress when you need HTTP routing. Add HPA when you need autoscaling. Build up complexity only as needed.
