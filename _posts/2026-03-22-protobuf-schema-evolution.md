---
layout: post
title: "Protobuf Schema Evolution: Backward and Forward Compatibility"
date: 2026-03-22 08:00:00 +0700
tags: [protobuf, grpc, schema-evolution, backend, distributed-systems]
description: "A decision matrix for categorizing Protobuf schema changes as safe, conditionally safe, or breaking — so you can evolve contracts without coordinated deployments."
---

You're running a gRPC service with 12 downstream consumers. A product requirement lands: add a new field to the response. You add it, deploy, and everything looks fine — until one service that hasn't upgraded yet starts returning zeros instead of the expected default, silently corrupting a billing calculation. No error. No log line. Just wrong data flowing through your system for six hours before someone notices. This is the Protobuf compatibility trap: the wire format is designed for evolution, but it will not protect you from your own misunderstanding of its rules. Knowing exactly which changes are safe, which are conditional, and which will destroy data in flight is the difference between rolling deployments and emergency rollbacks.

## The Wire Format Is the Contract

Protobuf doesn't serialize field names. It serializes field numbers paired with wire types. When a decoder encounters a field number it doesn't recognize, it skips it. When it expects a field that isn't present, it uses the default value. This is the entire foundation of compatibility — and the source of most bugs.

A field number is permanent. Once you assign field number `5` to `user_id`, that number means `user_id` for the lifetime of the proto definition. Changing what field number `5` represents is a breaking change, full stop. The decoder has no way to distinguish "this is a new field with number 5" from "this is the same field with a new type."

Wire types map to encoding families:

| Wire Type | Value | Encoding |
|-----------|-------|----------|
| VARINT    | 0     | int32, int64, uint32, uint64, sint32, sint64, bool, enum |
| I64       | 1     | fixed64, sfixed64, double |
| LEN       | 2     | string, bytes, embedded messages, repeated fields |
| I32       | 5     | fixed32, sfixed32, float |

Field number and wire type are packed together in a single byte as `(field_number << 3) | wire_type`. A decoder that gets wire type 0 but expects wire type 2 for the same field number will either throw a parse error or silently misread the data. Both outcomes are bad.

## Backward vs. Forward Compatibility Defined Precisely

These terms get conflated constantly. Be precise:

**Backward compatibility**: New code can read data written by old code. Old producers, new consumers.

**Forward compatibility**: Old code can read data written by new code. New producers, old consumers.

For rolling deployments — which is every zero-downtime deploy — you need both simultaneously. During the rollout window, you have instances of v1 and v2 running concurrently, sending and receiving messages to each other in both directions. A change that breaks either direction breaks your deployment.

## The Safe Changes

### Adding a New Field

Adding a new optional field with a new field number is the canonical safe operation.

<script src="https://gist.github.com/mohashari/e33c2953b394898c40803e65412fc035.js?file=snippet-1.txt"></script>

Old consumers receiving v2 messages skip fields 4 and 5 — they're unknown field numbers. New consumers receiving v1 messages get the zero value for `timezone` (empty string) and `created_at_unix` (0). If your application logic treats empty string as "timezone not set" and handles it gracefully, this is fully safe in both directions.

The trap is assuming "optional" means "safe to add without handling the zero value." In proto3, every scalar field is effectively optional with a zero default. If `created_at_unix = 0` is a valid business value in your system, you cannot distinguish "field not present" from "field is explicitly zero" without using `google.protobuf.Int64Value` or `optional` keyword in proto3 syntax.

### Removing a Field (With Reservations)

Deleting a field is safe on the wire — the decoder ignores unknown field numbers. What makes it unsafe is reusing the field number later.

<script src="https://gist.github.com/mohashari/e33c2953b394898c40803e65412fc035.js?file=snippet-2.txt"></script>

`reserved` is a compile-time guardrail. The `protoc` compiler will reject any future `.proto` file that reuses a reserved field number or name. Use it every time you remove a field. No exceptions.

## The Conditionally Safe Changes

### Renaming a Field

Field names don't appear on the wire. Renaming `user_id` to `account_id` in the `.proto` file has zero effect on binary serialization. The generated code changes, so all callers must recompile, but existing serialized bytes remain valid.

The risk is JSON transcoding. If you're using `grpc-gateway`, `protojson`, or any system that maps proto fields to JSON by name, a rename is a breaking change for those consumers. Run `grep` across your codebase for JSON field name references before renaming anything.

### Changing Between Compatible Numeric Types

Some type changes preserve the wire type and are safe:

<script src="https://gist.github.com/mohashari/e33c2953b394898c40803e65412fc035.js?file=snippet-3.txt"></script>

The `int32` to `int64` promotion is the most common case. Old writers produce values that fit in 32 bits. New readers decode them into 64-bit integers with zero issues. Going the other direction — truncating `int64` to `int32` — works silently until a value exceeds `2^31 - 1`, at which point you get silent data corruption, not an error.

### Changing Field Cardinality in proto3

In proto3, changing a field from singular to `repeated` or vice versa is dangerous:

- Singular to `repeated`: Old code writes one value. New code reads a list of one element. Generally fine for reading.
- `repeated` to singular: Old code writes a list. New code reads only the **last** element and discards the rest. Silent data loss.

The `repeated` to singular direction is a trap because it won't cause a parse error — the proto decoder is specified to take the last value for a singular field when multiple values are present on the wire. You won't know you're dropping data.

## The Breaking Changes

### Changing a Field's Wire Type

Any change that moves a field between wire type families is breaking. The decoder will either throw `proto: cannot parse invalid wire-format data` or, worse, misinterpret the bytes as a different type and produce garbage.

<script src="https://gist.github.com/mohashari/e33c2953b394898c40803e65412fc035.js?file=snippet-4.txt"></script>

This is the change that will cause production incidents at 3am. The symptoms look like random message corruption — some messages decode fine (those where the old varint encoding happens to produce bytes that are parseable as a float), some throw errors. It depends on the specific values in flight.

### Changing Enum Values

Enums serialize as `int32` on the wire. Adding new enum values is safe — old decoders preserve unknown enum values as their raw integer (proto3 behavior). Removing or renumbering enum values is breaking.

<script src="https://gist.github.com/mohashari/e33c2953b394898c40803e65412fc035.js?file=snippet-5.txt"></script>

The renumbering case is particularly nasty because an order that was `PROCESSING` (value 2) will be decoded as `SHIPPED` (which is now value 2) by old consumers. No error. Wrong state machine transition. Potentially an order that gets marked as shipped before it's been processed.

## Protobuf Schema Registries in Production

For teams running multiple services with shared proto definitions, a schema registry enforces compatibility at CI time rather than at 3am.

[Buf](https://buf.build/) is the dominant tool here. Its `buf breaking` command checks a proposed schema change against a baseline and categorizes it by compatibility impact.

```yaml
# snippet-6
# buf.yaml — breaking change detection configuration
version: v1
breaking:
  use:
    - FILE         # check file-level breaking changes (field removal, type change)
  except:
    - FIELD_SAME_DEFAULT  # allow default value changes (if you have a policy for this)

# .github/workflows/proto-check.yml — CI enforcement
# name: Proto Breaking Change Check
# on: [pull_request]
# jobs:
#   buf-breaking:
#     runs-on: ubuntu-latest
#     steps:
#       - uses: actions/checkout@v4
#       - uses: bufbuild/buf-action@v1
#         with:
#           breaking_against: "https://buf.build/yourorg/yourrepo:main"
```

`buf breaking` catches: field type changes, field number reuse, required field additions (proto2), field removal without reservation. It does not catch semantic breakage — a field rename that breaks JSON consumers, or a `repeated` to singular conversion that drops data. Those require human review.

## The Decision Matrix

Before committing any schema change, run it through this matrix:

| Change | Wire Safe | Semantic Safe | Verdict |
|--------|-----------|---------------|---------|
| Add field with new number | Yes | Conditional (handle zero values) | Safe |
| Remove field + reserve number | Yes | Yes | Safe |
| Rename field | Yes | Conditional (check JSON consumers) | Conditionally Safe |
| int32 → int64 (same number) | Yes | Conditional (no overflow) | Conditionally Safe |
| singular → repeated | Yes | Conditional (one-element list OK) | Conditionally Safe |
| repeated → singular | Yes | No (drops all but last) | Breaking |
| Change wire type (same number) | No | No | Breaking |
| Reuse reserved field number | No | No | Breaking |
| Renumber enum values | Yes (wire) | No (semantic) | Breaking |
| Remove enum value | Yes (wire) | No (unknown int stored) | Breaking |

"Wire safe" means the binary parser won't throw. "Semantic safe" means the decoded values are correct for your application logic. You need both.

## Testing Compatibility in CI

The most reliable test is encoding with v1, decoding with v2, and verifying the output — and the reverse. Here's a Go test pattern that makes this explicit:

<script src="https://gist.github.com/mohashari/e33c2953b394898c40803e65412fc035.js?file=snippet-7.go"></script>

Run these tests in CI for every proto change. Keep both the old and new generated code in the test package simultaneously. This is the only way to mechanically verify that a change is safe during a rolling deployment — not just theoretically safe, but verified against actual wire bytes.

## Versioning Strategy for Breaking Changes

Sometimes you genuinely need a breaking change — a field needs to move to a nested message, or a string field needs to become a message type for richer structure. The correct approach is package versioning, not in-place mutation.

Define `yourapp.users.v1` and `yourapp.users.v2` as separate packages. Run both versions simultaneously. Migrate consumers to v2 incrementally. Deprecate v1 with an explicit sunset date. Delete v1 after all consumers are off.

This is more operational overhead than patching a field in place, but it's the only approach that gives you complete control over the migration timeline without coordinating deployments across a dozen services simultaneously. The alternative — attempting an in-place breaking change with a coordinated cutover — scales directly with the number of consumers and fails in proportion to how many are maintained by other teams.

The proto field number contract is not a suggestion. It is the wire format. Treat it like a public API: additive changes are cheap, mutations are expensive, and deletions require a migration path.
```