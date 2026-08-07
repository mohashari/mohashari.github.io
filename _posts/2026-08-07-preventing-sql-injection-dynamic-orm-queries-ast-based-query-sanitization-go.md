---
layout: post
title: "Preventing SQL Injection in Dynamic ORM Queries: AST-Based Query Sanitization in Go"
date: 2026-08-07 08:00:00 +0700
tags: [go, security, devsecops, orm]
description: "Learn how to build secure dynamic query engines in Go by parsing client-side inputs into ASTs and enforcing strict schema and structure validation."
image: "https://picsum.photos/seed/6731/1080/720"
thumbnail: "https://picsum.photos/seed/6731/400/300"
---

During a routine security audit of a high-throughput multi-tenant SaaS dashboard, we uncovered a critical blind spot that compromised our data isolation boundary. A tenant user managed to extract cross-tenant transaction records by manipulating a seemingly benign "sort column" dropdown. The backend, built with Go and a popular Object-Relational Mapper (ORM), dynamically fed the client-supplied column name directly into an `.Order()` clause. Because SQL engines do not allow column names to be parameterized, the query engine executed the raw string, exposing the database to SQL injection (SQLi). This wasn't a failure of database/sql parameterization; it was a failure to understand the limits of ORM protection. This post dissects why traditional query construction fails when queries are dynamic, and demonstrates how to build an Abstract Syntax Tree (AST) sanitization pipeline to achieve absolute query safety without sacrificing API flexibility.

## The Escape Hatch Trap: Why ORMs Fail at Dynamic Queries

Modern Go ORMs like GORM, Ent, and SQLx provide robust protection against SQL injection by using prepared statements and parameterized inputs for SQL values. When you write `db.Where("email = ?", input)`, the SQL driver transmits the query structure and the variable data separately to the database engine. The value of `input` is never parsed as SQL command text.

However, complex production applications often require dynamic queries where the structures themselves are user-defined. Enterprise dashboards, advanced search panels, and telemetry tools allow users to select which columns to display, how to filter them, and how to sort the output. To facilitate this, ORMs provide "escape hatches"—methods that accept raw SQL fragments. 

In GORM, methods like `.Order()`, `.Select()`, `.Group()`, and `.Having()` do not accept parameter placeholders in the same way `.Where()` does. They interpolate the string arguments directly into the generated SQL statement. 

<script src="https://gist.github.com/mohashari/df8b98dcb71c7bd63df5013a3fa1e4ca.js?file=snippet-1.go"></script>

If an attacker passes `price; SELECT pg_sleep(10)` or inserts nested SELECT statements into the `sortColumn` parameter, the database will execute the arbitrary SQL code. Standard libraries cannot parameterize these positions because database engines require column names and sort directions to compile the query plan before values are evaluated.

## The Failure of String-Based Whitelisting

The naive fix for dynamic query input is string whitelisting or regular expression checking. Developers write filters to ensure that the column name matches `^[a-zA-Z0-9_]+$`. While this protects simple sorting, it breaks down quickly under the requirements of a modern API:

1. **Complex Joins**: If a filter needs to reference a joined table, the query column identifier might look like `categories.name` or `creator_user.profile_url`. Relaxing the regex to allow dots (`.`) immediately reintroduces injection vectors.
2. **Dynamic Operators**: If users can choose comparison operators (e.g., `=`, `>`, `<=`, `LIKE`, `IN`), validating the operators via simple string containment leads to complex regex structures that are highly prone to bypasses (e.g., using SQL comment syntax `/**/` to bypass token checks).
3. **Scale and Maintenance**: Across a codebase of 200+ REST endpoints, maintaining manual string validations becomes an operational nightmare. A developer forgets to validate a single field, and the entire database is exposed.

To solve this safely, we must separate the user's intent from the query generation. We do this by treating the incoming query as code, parsing it into an Abstract Syntax Tree (AST), validating the nodes of the tree against a strict schema, and compiling the safe tree back into SQL builder actions.

## Designing the Query AST in Go

An Abstract Syntax Tree represents the structure of an expression as a tree of nodes. In our dynamic query engine, we want to allow users to build expressions like:

`price > 100 AND (status = 'active' OR category = 'books')`

This expression can be parsed into a tree structure. We define our AST nodes using interfaces and structs in Go to represent logical operators, comparisons, table/column identifiers, and literal values.

<script src="https://gist.github.com/mohashari/df8b98dcb71c7bd63df5013a3fa1e4ca.js?file=snippet-2.go"></script>

By defining the query structure recursively, we prevent the user from passing raw SQL statements. An attacker cannot inject raw SQL commands because the AST structure only knows how to represent structured conditions, not arbitrary statements like `UNION` or `DROP TABLE`.

## The Schema-Driven AST Validator

Once we parse the incoming input into our AST, we must validate it. We cannot trust that the fields, operators, or values are safe just because they are structured. For example, a user could query internal admin tables or attempt a Denial of Service (DoS) attack by passing a deeply nested tree.

To prevent this, we write a validator using the Visitor pattern. The validator ensures that:
- The nesting depth of the AST does not exceed a safe limit (preventing Stack Overflow panic attacks).
- The query fields are strictly whitelisted for the current resource.
- The comparison operators are valid for the specific field (e.g., you cannot run a `LIKE` query on an integer price field).
- The types of the query values match the database schema types.

<script src="https://gist.github.com/mohashari/df8b98dcb71c7bd63df5013a3fa1e4ca.js?file=snippet-3.go"></script>

## Compiling the AST to Safe SQL

Once the AST passes validation, we know that:
1. Every field name is a whitelisted string defined in our schema.
2. Every operator is in the schema's allowed list.
3. Every literal value matches the target type.

Now we can safely compile this tree into a GORM dynamic query builder. When generating SQL, we interpolate the *identifiers* (which are safe because they matched our whitelist) and parameterize the *literals* (by passing them as placeholder arguments to GORM).

<script src="https://gist.github.com/mohashari/df8b98dcb71c7bd63df5013a3fa1e4ca.js?file=snippet-4.go"></script>

## API Implementation: Integrating AST Parsing & Validation

To test this engine in production, we require an endpoint that accepts a structured filter payload, converts it to an AST, validates it, and compiles it. The dynamic search filter payload can be modeled as a JSON object that maps recursive sub-expressions.

<script src="https://gist.github.com/mohashari/df8b98dcb71c7bd63df5013a3fa1e4ca.js?file=snippet-5.go"></script>

## Securing Dynamic Ordering and Sorting

Sorting requires a different validation model than filtering. Filters output logical boolean expressions (where clauses), while sorting applies ordering clauses. Because you cannot use the `ORDER BY ?` parameterized placeholder directly in database drivers, you must perform validation before sorting dynamic queries.

<script src="https://gist.github.com/mohashari/df8b98dcb71c7bd63df5013a3fa1e4ca.js?file=snippet-6.go"></script>

## Performance & Optimization in Production

Introducing AST parsing and validation adds memory allocation overhead. Under heavy workloads, executing garbage collector operations for hundreds of parsed queries per second can degrade system latency.

### 1. Reusing Allocations with sync.Pool

We can reduce memory allocation by using a `sync.Pool` to reuse our Validator instances and parsing buffers, preventing memory re-allocation under high throughput.

### 2. Caching AST Compile Runs

If you process repetitive query structures, you can cache the resulting string format structures. By converting the AST (minus literal values) into a structural key hash (e.g., `(category = ? AND status = ?)`), you can query a thread-safe cache (`sync.Map` or an LRU cache implementation) to skip parsing and validation steps for identical structural patterns.

### 3. Enforcing AST Depth

A deep AST can exhaust the Go call stack, causing the application thread to panic. Enforce strict depth validations (e.g., maximum depth of 5 to 10 nodes) at the API parsing level. This prevents resource exhaustion attacks where clients send deeply nested logical JSON objects.

## Static Analysis and CI/CD Guardrails

Manual reviews are prone to developer oversight. To prevent dynamic ORM injection risks from reaching production, integrate automated static analysis tools into your CI/CD pipeline.

Use `gosec` (Go Security Checker) in your lint execution setups. The rules `G201` and `G202` trace string formatting and concatenation in database operations:

```yaml
# snippet-7
# .github/workflows/security.yml
name: Security Scan
on: [push, pull_request]

jobs:
  gosec:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Source
        uses: actions/checkout@v4

      - name: Run Gosec Security Audit
        uses: securego/gosec@master
        with:
          # G201: SQL query construction using format string
          # G202: SQL query construction using string concatenation
          args: '-include=G201,G202 ./...'
```

If a developer attempts to bypass parameters by concatenating strings inside database query steps, the `gosec` build step will fail, preventing the code from merging into production.

## Summary

Dynamic querying is a common product requirement that exposes applications to SQL injection if handled incorrectly. By adopting AST-based query sanitization:

1. **Untrusted inputs** are validated against a strict schema mapping before query execution.
2. **Identifiers** are verified using whitelist schemas, removing the risk of command injections in unbindable query parts like ordering clauses.
3. **Parameters** are handled via standard driver bindings.
4. **CI/CD linters** block raw string operations, ensuring developers use safe parsing structures.

Designing validation logic around an AST is a robust way to support complex APIs without compromising database security.