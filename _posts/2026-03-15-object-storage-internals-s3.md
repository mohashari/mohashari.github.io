---
layout: post
title: "Object Storage Internals: S3-Compatible Systems and When to Use Them"
date: 2026-03-15 07:00:00 +0700
tags: [object-storage, s3, cloud, architecture, backend]
description: "Understand the architecture of S3-compatible object storage, consistency models, and patterns for storing large-scale unstructured data."
---

# Object Storage Internals: S3-Compatible Systems and When to Use Them

Every backend engineer eventually hits the same wall: a filesystem that can't scale, a database storing blobs it was never designed to hold, or an NFS mount that becomes a single point of failure at the worst possible moment. The moment your service needs to store terabytes of user-uploaded content, machine learning model artifacts, or audit log archives, you need a storage primitive that decouples durability from compute — one that can scale horizontally without resharding, survive datacenter failures, and serve billions of objects without a DBA involved. That primitive is object storage, and understanding its internals will fundamentally change how you design data-intensive systems.

## What Object Storage Actually Is

Unlike a filesystem (hierarchical, POSIX-compliant, inode-based) or a block device (fixed-size sectors, stateful connections), object storage is a flat namespace of key-value pairs. An object consists of an opaque blob of bytes, a globally unique key, and a metadata envelope. There is no concept of directories — what looks like `logs/2026/03/app.log` is just a string prefix. The storage layer is responsible for durability and availability; your application is responsible for key design.

The S3 API became the de facto standard because it exposed this model over plain HTTP with a sensible REST interface. Today, nearly every object storage system — MinIO, Ceph RadosGW, Backblaze B2, Cloudflare R2, Wasabi, and cloud-native offerings from every major provider — speaks the S3 protocol. This portability is its most underappreciated feature.

## Consistency Model: Then and Now

Historically, S3 offered eventual consistency for overwrites and deletes, which surprised engineers who expected read-your-writes semantics. In 2020, AWS announced strong read-after-write consistency for all operations. MinIO and most modern S3-compatible systems followed suit. However, if you are running Ceph or another distributed system in a multi-site replication configuration, understanding your consistency zone boundary matters for correctness.

The practical implication: do not use object storage as a coordination primitive. Don't implement a distributed lock by racing to PUT the same key. Use it for what it's good at — durable, immutable-ish storage of large blobs with predictable throughput.

## Connecting to Any S3-Compatible Endpoint

The AWS SDK's endpoint override is your portability lever. The same client code works against AWS, MinIO, or a local emulator like LocalStack, which makes local development and CI identical to production.

This Go snippet initializes a client that can target any S3-compatible endpoint by injecting the base URL via configuration:

<script src="https://gist.github.com/mohashari/2f4f83c14b0b3d3b09d4ce58effb3ff4.js?file=snippet.go"></script>

`UsePathStyle = true` is critical for self-hosted systems — virtual-hosted-style URLs (`bucket.hostname`) require DNS wildcard configuration that most private deployments don't have.

## Multipart Uploads for Large Objects

Single PUT requests are limited to 5 GB on most systems and are fragile over unreliable networks. For anything above a few hundred megabytes, use multipart upload. It parallelizes the transfer, allows resumability, and significantly improves throughput.

The following uploads a large file by splitting it into 10 MB chunks and uploading them concurrently:

<script src="https://gist.github.com/mohashari/2f4f83c14b0b3d3b09d4ce58effb3ff4.js?file=snippet-2.go"></script>

Under the hood, multipart upload works in three phases: `CreateMultipartUpload` returns an upload ID, each `UploadPart` returns an ETag, and `CompleteMultipartUpload` atomically assembles them. Aborted uploads leave orphaned parts that accumulate storage costs — always configure a lifecycle rule to clean them up.

## Lifecycle Rules to Control Storage Costs

One of the most powerful and underused features of object storage is automatic lifecycle management. Transitioning objects to cheaper storage classes (or deleting them outright) based on age eliminates entire categories of manual data hygiene.

This bucket policy transitions infrequently accessed data to a cold tier after 30 days and expires it after a year. It also cleans up incomplete multipart uploads after 7 days:

<script src="https://gist.github.com/mohashari/2f4f83c14b0b3d3b09d4ce58effb3ff4.js?file=snippet-3.json"></script>

Apply this via the AWS CLI or SDK. For MinIO, use `mc ilm import` with an equivalent structure.

## Presigned URLs for Secure, Direct Client Uploads

A common anti-pattern is routing file uploads through your application server, which wastes bandwidth and compute. Presigned URLs let your backend generate a time-limited, capability-bearing URL that the client uses to upload or download directly from the object store — no proxy required.

<script src="https://gist.github.com/mohashari/2f4f83c14b0b3d3b09d4ce58effb3ff4.js?file=snippet-4.go"></script>

Your API generates the presigned URL, returns it to the client with the expected object key, and then the client makes a direct PUT to the storage system. After the client reports completion, your backend can verify the object exists and record its metadata in your database.

## Event Notifications for Reactive Pipelines

Object storage systems support event notifications — webhooks or queue messages fired when objects are created, deleted, or restored. This turns your storage layer into an event source, enabling decoupled processing pipelines without polling.

Configure MinIO bucket notifications to push to an NATS or Kafka topic, or use S3's native integration with SQS and Lambda. Here is a shell snippet using the AWS CLI to set up an SQS notification for all object creation events in a prefix:

<script src="https://gist.github.com/mohashari/2f4f83c14b0b3d3b09d4ce58effb3ff4.js?file=snippet-5.sh"></script>

When a `.parquet` file lands under `raw/`, your consumer triggers a transformation job. This pattern is the backbone of modern data lake ingestion pipelines.

## Running MinIO Locally for Development

Replacing cloud dependencies in development with a local MinIO instance eliminates flakiness and cost. The following Docker Compose fragment brings up a MinIO instance with a persistent volume and a fixed set of credentials that your application config can reference:

<script src="https://gist.github.com/mohashari/2f4f83c14b0b3d3b09d4ce58effb3ff4.js?file=snippet-6.yaml"></script>

Set `OBJECT_STORAGE_ENDPOINT=http://localhost:9000` in your local environment and use the same client code that targets production. Your integration tests run against a real S3-compatible system with zero cloud egress fees.

## When to Use Object Storage — and When Not To

Object storage excels at large, immutable, write-once-read-many workloads: user uploads, build artifacts, database backups, log archives, ML training datasets, and static asset serving. Its economics are exceptional at scale — sub-cent-per-GB-month pricing with no IOPS charges on most platforms.

It is a poor fit for high-frequency small-object writes (thousands of tiny files per second), workloads requiring strong transactional semantics across multiple objects, or use cases needing sub-millisecond latency. For those patterns, reach for a block device, a local SSD, or a purpose-built database instead.

The shift in thinking required is this: object storage is not a slower filesystem. It is a different abstraction optimized for different constraints. Once you internalize that its key design, access patterns, and lifecycle configuration are first-class concerns in your architecture — not afterthoughts bolted on at deployment — you will stop fighting its model and start building systems that are cheaper, more durable, and dramatically easier to operate at scale.