---
layout: post
title: "Terraform for Backend Engineers: Infrastructure as Code That Actually Works"
date: 2026-03-15 07:00:00 +0700
tags: [terraform, devops, infrastructure, cloud, backend]
description: "Learn Terraform from the ground up — providers, state management, modules, workspaces, and production patterns for managing cloud infrastructure safely."
---

Manual infrastructure creates snowflake servers — nobody knows exactly what's configured, changes aren't reviewed, and rollbacks are impossible. Terraform solves this by treating infrastructure like application code: versioned, reviewable, and reproducible.

## Core Concepts

- **Provider**: Plugin that manages a specific platform (AWS, GCP, Kubernetes)
- **Resource**: A managed infrastructure component (`aws_instance`, `google_sql_database`)
- **State**: Terraform's record of what infrastructure it created
- **Plan**: Preview of changes before applying
- **Module**: Reusable infrastructure component

## First Configuration — AWS EC2 + RDS

<script src="https://gist.github.com/mohashari/67090e43c0d7c1ee4dc2dba7fb0628a6.js?file=snippet.hcl"></script>

## Networking — VPC and Subnets

<script src="https://gist.github.com/mohashari/67090e43c0d7c1ee4dc2dba7fb0628a6.js?file=snippet-2.hcl"></script>

## RDS PostgreSQL

<script src="https://gist.github.com/mohashari/67090e43c0d7c1ee4dc2dba7fb0628a6.js?file=snippet-3.hcl"></script>

## Modules — Reusable Infrastructure

<script src="https://gist.github.com/mohashari/67090e43c0d7c1ee4dc2dba7fb0628a6.js?file=snippet.txt"></script>

<script src="https://gist.github.com/mohashari/67090e43c0d7c1ee4dc2dba7fb0628a6.js?file=snippet-4.hcl"></script>

## Workspaces — Environments as Code

<script src="https://gist.github.com/mohashari/67090e43c0d7c1ee4dc2dba7fb0628a6.js?file=snippet.sh"></script>

## The Safe Production Workflow

<script src="https://gist.github.com/mohashari/67090e43c0d7c1ee4dc2dba7fb0628a6.js?file=snippet-2.sh"></script>

## Sensitive State — Never Commit It

State files contain plaintext secrets (DB passwords, API keys). Always use remote state:

<script src="https://gist.github.com/mohashari/67090e43c0d7c1ee4dc2dba7fb0628a6.js?file=snippet-5.hcl"></script>

Never run `terraform state` commands in production without a backup.

## Import Existing Infrastructure

<script src="https://gist.github.com/mohashari/67090e43c0d7c1ee4dc2dba7fb0628a6.js?file=snippet-3.sh"></script>

Terraform turns "I clicked this in the console" into auditable, reproducible code. Start with remote state, use modules for anything you deploy more than once, and always plan before applying.
