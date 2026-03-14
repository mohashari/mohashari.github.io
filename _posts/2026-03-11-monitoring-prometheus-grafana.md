---
layout: post
title: "Monitoring Your Backend with Prometheus and Grafana"
tags: [monitoring, prometheus, grafana, devops, backend]
description: "Set up production-grade observability for your backend services using Prometheus metrics and Grafana dashboards."
---

You can't improve what you can't measure. Prometheus + Grafana is the gold standard for backend observability. This guide gets you from zero to a production-ready monitoring stack.

## The Four Golden Signals

Before instrumenting anything, know what to measure:

1. **Latency** — How long requests take (distinguish success vs error latency)
2. **Traffic** — How many requests per second
3. **Errors** — Rate of failed requests
4. **Saturation** — How "full" your service is (CPU, memory, queue depth)

Everything else is secondary.

## Setting Up Prometheus

```yaml
# docker-compose.yml
version: '3.8'
services:
  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.retention.time=30d'
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana:latest
    volumes:
      - grafana_data:/var/lib/grafana
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=secret
    ports:
      - "3000:3000"

volumes:
  prometheus_data:
  grafana_data:
```

```yaml
# prometheus.yml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'my-api'
    static_configs:
      - targets: ['api:8080']
    metrics_path: '/metrics'
```

## Instrumenting Your Go Service

```go
import (
    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promauto"
    "github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
    httpRequestsTotal = promauto.NewCounterVec(
        prometheus.CounterOpts{
            Name: "http_requests_total",
            Help: "Total HTTP requests",
        },
        []string{"method", "path", "status"},
    )

    httpRequestDuration = promauto.NewHistogramVec(
        prometheus.HistogramOpts{
            Name:    "http_request_duration_seconds",
            Help:    "HTTP request duration in seconds",
            Buckets: prometheus.DefBuckets,  // .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10
        },
        []string{"method", "path"},
    )

    activeConnections = promauto.NewGauge(prometheus.GaugeOpts{
        Name: "active_connections",
        Help: "Number of active connections",
    })

    dbQueryDuration = promauto.NewHistogramVec(
        prometheus.HistogramOpts{
            Name:    "db_query_duration_seconds",
            Help:    "Database query duration",
            Buckets: []float64{.001, .005, .01, .025, .05, .1, .25, .5, 1},
        },
        []string{"query"},
    )
)

// Middleware to auto-instrument HTTP handlers
func MetricsMiddleware(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        start := time.Now()

        rw := &responseWriter{ResponseWriter: w, statusCode: 200}
        next.ServeHTTP(rw, r)

        duration := time.Since(start).Seconds()
        status := strconv.Itoa(rw.statusCode)

        httpRequestsTotal.WithLabelValues(r.Method, r.URL.Path, status).Inc()
        httpRequestDuration.WithLabelValues(r.Method, r.URL.Path).Observe(duration)
    })
}

// Expose metrics endpoint
http.Handle("/metrics", promhttp.Handler())
```

## Business Metrics

Beyond HTTP metrics, track business-level metrics:

```go
var (
    ordersCreated = promauto.NewCounterVec(prometheus.CounterOpts{
        Name: "orders_created_total",
        Help: "Total orders created",
    }, []string{"payment_method", "tier"})

    orderValue = promauto.NewHistogramVec(prometheus.HistogramOpts{
        Name:    "order_value_dollars",
        Help:    "Distribution of order values",
        Buckets: []float64{1, 5, 10, 25, 50, 100, 250, 500, 1000},
    }, []string{"tier"})

    queueDepth = promauto.NewGaugeVec(prometheus.GaugeOpts{
        Name: "job_queue_depth",
        Help: "Number of jobs waiting in queue",
    }, []string{"queue"})
)

func CreateOrder(ctx context.Context, order Order) error {
    if err := db.CreateOrder(ctx, order); err != nil {
        return err
    }

    // Track metrics
    ordersCreated.WithLabelValues(order.PaymentMethod, order.UserTier).Inc()
    orderValue.WithLabelValues(order.UserTier).Observe(order.TotalUSD)
    return nil
}
```

## Essential PromQL Queries

```promql
# Request rate (per second over 5-minute window)
rate(http_requests_total[5m])

# Error rate percentage
sum(rate(http_requests_total{status=~"5.."}[5m])) /
sum(rate(http_requests_total[5m])) * 100

# P95 and P99 latency
histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))
histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[5m]))

# 95th percentile by endpoint
histogram_quantile(0.95,
  sum by (path, le) (
    rate(http_request_duration_seconds_bucket[5m])
  )
)

# CPU usage
100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)

# Memory usage
(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100
```

## Alerting Rules

```yaml
# alerts.yml
groups:
  - name: api-alerts
    rules:
      - alert: HighErrorRate
        expr: |
          sum(rate(http_requests_total{status=~"5.."}[5m])) /
          sum(rate(http_requests_total[5m])) > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "High error rate: {{ $value | humanizePercentage }}"

      - alert: HighLatency
        expr: |
          histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "P95 latency above 1s: {{ $value }}s"

      - alert: ServiceDown
        expr: up == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Service {{ $labels.job }} is down"
```

## Grafana Dashboard JSON

Create dashboards as code using Grafana's JSON model and store them in version control. Use the Grafana dashboard provisioning:

```yaml
# grafana/provisioning/dashboards/dashboard.yml
apiVersion: 1
providers:
  - name: 'default'
    folder: ''
    type: file
    options:
      path: /var/lib/grafana/dashboards
```

## The Three Pillars of Observability

Metrics alone aren't enough:

| Pillar | Tool | Use For |
|--------|------|---------|
| **Metrics** | Prometheus + Grafana | Trends, dashboards, alerting |
| **Logs** | Loki / Elasticsearch | Debugging, search, audit |
| **Traces** | Jaeger / Tempo | Distributed request flow |

Add structured logging and distributed tracing alongside your metrics for complete observability.

Start with the four golden signals. Get alerts working. Then expand to business metrics. You don't need everything on day one.
