---
layout: post
title: "Docker Containerization Best Practices for Production"
tags: [docker, devops, backend]
description: "Level up your Docker skills with proven patterns for writing lean, secure, and production-ready container images."
---

Docker has become the de-facto standard for packaging and shipping applications. But running containers in production requires more than just `docker run`. Here are the practices that separate hobby projects from production-grade deployments.

## 1. Use Multi-Stage Builds

Multi-stage builds dramatically reduce final image size by separating the build environment from the runtime environment.

```dockerfile
# Build stage
FROM golang:1.21-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o server ./cmd/server

# Runtime stage
FROM alpine:3.19
RUN apk --no-cache add ca-certificates tzdata
WORKDIR /root/
COPY --from=builder /app/server .
CMD ["./server"]
```

The result? A final image of ~15MB instead of 800MB.

## 2. Pin Your Base Image Versions

Never use `latest` in production. Pin to specific digest hashes for reproducibility:

```dockerfile
FROM node:20.11-alpine3.19@sha256:abc123...
```

This prevents unexpected breakage when upstream images change.

## 3. Run as Non-Root

By default containers run as root — a massive security risk. Always create and switch to a non-root user:

```dockerfile
RUN addgroup -g 1001 appgroup && \
    adduser -u 1001 -G appgroup -s /bin/sh -D appuser
USER appuser
```

## 4. Layer Your COPY Instructions Wisely

Docker caches layers. Put things that change less frequently earlier:

```dockerfile
# Dependencies first (changes rarely)
COPY package.json package-lock.json ./
RUN npm ci --only=production

# Source code last (changes often)
COPY src/ ./src/
```

## 5. Use .dockerignore

Always include a `.dockerignore` to keep your build context lean:

```
node_modules
.git
.env
*.log
dist
coverage
```

## 6. Set Resource Limits

Don't let one container eat all your host resources:

```yaml
# docker-compose.yml
services:
  api:
    image: myapp:latest
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 512M
        reservations:
          memory: 256M
```

## 7. Health Checks

Define health checks so orchestrators know your container's true state:

```dockerfile
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://localhost:8080/health || exit 1
```

## 8. Use Read-Only Filesystems

Mount the container filesystem as read-only and explicitly allow writable paths:

```bash
docker run --read-only \
  --tmpfs /tmp \
  --tmpfs /var/run \
  myapp:latest
```

## 9. Scan Images for Vulnerabilities

Integrate image scanning in CI:

```bash
# Using Trivy
trivy image --exit-code 1 --severity HIGH,CRITICAL myapp:latest
```

## 10. Keep Secrets Out of Images

Never bake secrets into images. Use Docker secrets or environment variables injected at runtime:

```bash
docker run -e DATABASE_URL="$(cat /run/secrets/db_url)" myapp:latest
```

## Summary

| Practice | Impact |
|----------|--------|
| Multi-stage builds | Smaller images |
| Non-root user | Security |
| Layer caching | Faster builds |
| Health checks | Reliability |
| Image scanning | Security posture |

Following these practices means your containers are lean, secure, and battle-hardened for the real world.
