---
layout: post
title: "Protocol Buffers Deep Dive: Schema Design, Evolution, and Versioning"
date: 2026-03-16 07:00:00 +0700
tags: [protobuf, grpc, apis, serialization, backend]
description: "Go beyond the basics to understand how to design backward-compatible Protobuf schemas that evolve safely across services and deployments."
---

Every backend engineer has faced this moment: you need to add a field to a message your service sends to three other services, and you're not sure which ones you can deploy first. Change the wrong thing, and you'll break a consumer. Deploy in the wrong order, and you have a window where serialization silently drops data. Protocol Buffers were designed to make schema evolution safe, but "safe" is only guaranteed if you understand the rules. Most engineers learn the happy path — define a `.proto` file, generate code, ship it — and never dig into the mechanics that make backward and forward compatibility work. This post is that dig.

## How Wire Format Actually Works

Before you can evolve a schema safely, you need a mental model of what Protobuf actually serializes. Each field on the wire is identified by its **field number**, not its name. The name is purely for generated code. Every encoded field is a tag-value pair: the tag encodes the field number and a wire type (varint, 64-bit, length-delimited, or 32-bit). When a deserializer encounters a field number it doesn't recognize, it reads past it using the wire type — it doesn't fail. This is the foundation of forward compatibility.

<script src="https://gist.github.com/mohashari/201d54365789b00502c3d02a98119ec8.js?file=snippet.proto"></script>

The field numbers `1`, `2`, `3`, `5` are permanent contracts. If you ship a consumer compiled against this schema, and a producer later adds `string phone_number = 6`, the consumer ignores tag 6 gracefully. That's forward compatibility. If the producer omits `full_name`, the consumer gets the zero value (`""`). That's backward compatibility. Neither side crashes.

## The Reserved Keyword Is Not Optional

Deleting a field is where engineers cause the most damage. If you delete field 4 and a future engineer reuses number 4 for a new field with a different type, you've created a silent data corruption bug. Old producers writing a `string` for field 4 will be decoded by new consumers expecting an `int64` — and proto3 will not save you, because the wire types may coincidentally parse without error while producing garbage values.

<script src="https://gist.github.com/mohashari/201d54365789b00502c3d02a98119ec8.js?file=snippet-2.proto"></script>

Reserve both the number and the name. The name reservation prevents a future `reserved 4` from being bypassed by someone who adds a field with the same name (which the compiler would otherwise allow, since proto3 name-checks are loose in some toolchains).

## Designing for Evolution from the Start

One of the highest-leverage habits is wrapping primitive fields in messages early, even when you don't need to. A bare `string address` is a dead end — you can never add structure to it without a breaking change. A message `Address` can grow indefinitely.

<script src="https://gist.github.com/mohashari/201d54365789b00502c3d02a98119ec8.js?file=snippet-3.proto"></script>

The nested message costs one extra length-delimited tag on the wire — negligible. The flexibility gain is enormous.

## Using oneof for Variant Types

A common mistake is modeling a discriminated union with multiple optional fields and documentation that says "only one will be set." `oneof` encodes this constraint in the schema, generates code that enforces mutual exclusivity, and makes the intent machine-readable for documentation generators and linters.

<script src="https://gist.github.com/mohashari/201d54365789b00502c3d02a98119ec8.js?file=snippet-4.proto"></script>

Fields inside a `oneof` cannot be `repeated`, and you can never move a field into or out of a `oneof` without breaking compatibility — treat the oneof boundary as permanent.

## Versioning Services, Not Just Messages

Schema evolution handles message-level changes, but service-level breaking changes — removing an RPC, renaming a method, changing semantics — require a version in the package path. This is the convention established by the Buf Style Guide and adopted by Google's own APIs.

<script src="https://gist.github.com/mohashari/201d54365789b00502c3d02a98119ec8.js?file=snippet-5.proto"></script>

Run both versions concurrently during the migration window. Consumer services opt into `v2` on their own schedule. Once all consumers are migrated and confirmed in production, deprecate `v1`.

## Enforcing Rules with buf

Manually auditing `.proto` diffs for breaking changes is error-prone. `buf` is the standard tool for this — it compares your current schema against a baseline (from a registry, git tag, or local directory) and fails CI on any breaking change.

<script src="https://gist.github.com/mohashari/201d54365789b00502c3d02a98119ec8.js?file=snippet-6.yaml"></script>

<script src="https://gist.github.com/mohashari/201d54365789b00502c3d02a98119ec8.js?file=snippet-7.sh"></script>

If a developer removes a field without reserving its number, renames a message, or changes a field's type, `buf breaking` fails with a precise error and the relevant `.proto` path. Wire this into your pull request pipeline before any `.proto` change reaches main.

## Generating and Pinning Code

Generated code should be committed to your repository — or at minimum reproduced deterministically from a pinned `buf.lock`. Floating generation means a toolchain upgrade can change the generated API surface without any `.proto` change, making it impossible to `git blame` an API regression.

<script src="https://gist.github.com/mohashari/201d54365789b00502c3d02a98119ec8.js?file=snippet-8.sh"></script>

The `require_unimplemented_servers=true` option is worth calling out: it makes your gRPC server fail to compile if you add a new RPC to the `.proto` but forget to implement it in your handler — catching a class of runtime errors at compile time.

Protocol Buffers give you a remarkably strong compatibility story, but only if you treat field numbers as immutable, reserve deleted fields aggressively, wrap primitives in messages when growth is plausible, and automate compatibility checks in CI with `buf`. The engineers who get burned by Protobuf evolution almost always skipped one of these steps under time pressure. Build these habits into your team's `.proto` review checklist now, and schema evolution becomes the low-drama operation it was always designed to be.