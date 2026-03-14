---
layout: post
title: "TypeScript for Backend Engineers: Beyond the Basics"
tags: [typescript, nodejs, backend]
description: "Advanced TypeScript patterns for building robust, maintainable backend services — type guards, discriminated unions, generic utilities, and more."
---

TypeScript's type system is far more powerful than most engineers use. If you're still just writing `interface Foo { bar: string }`, there's a whole world of type-level programming that can make your backend code safer and more expressive. Let's explore it.

## Discriminated Unions for Type-Safe State

Model states that can't coexist using discriminated unions:


<script src="https://gist.github.com/mohashari/cf0ac9572635ff473fa0f7320a445179.js?file=snippet.ts"></script>


## Type Guards for Runtime Safety


<script src="https://gist.github.com/mohashari/cf0ac9572635ff473fa0f7320a445179.js?file=snippet-2.ts"></script>


## Utility Types You Should Know


<script src="https://gist.github.com/mohashari/cf0ac9572635ff473fa0f7320a445179.js?file=snippet-3.ts"></script>


## Generic Constraints for Reusable Functions


<script src="https://gist.github.com/mohashari/cf0ac9572635ff473fa0f7320a445179.js?file=snippet-4.ts"></script>


## Result Type: Safe Error Handling

Replace try/catch with explicit Result types:


<script src="https://gist.github.com/mohashari/cf0ac9572635ff473fa0f7320a445179.js?file=snippet-5.ts"></script>


## Zod for Runtime Validation

TypeScript types are erased at runtime. Use Zod to validate external data:


<script src="https://gist.github.com/mohashari/cf0ac9572635ff473fa0f7320a445179.js?file=snippet-6.ts"></script>


## Template Literal Types


<script src="https://gist.github.com/mohashari/cf0ac9572635ff473fa0f7320a445179.js?file=snippet-7.ts"></script>


TypeScript's type system is Turing-complete. You can encode complex business rules at the type level, making impossible states unrepresentable and catching entire categories of bugs before they reach production.
