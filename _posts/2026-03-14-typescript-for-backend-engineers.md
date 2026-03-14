---
layout: post
title: "TypeScript for Backend Engineers: Beyond the Basics"
tags: [typescript, nodejs, backend]
description: "Advanced TypeScript patterns for building robust, maintainable backend services — type guards, discriminated unions, generic utilities, and more."
---

TypeScript's type system is far more powerful than most engineers use. If you're still just writing `interface Foo { bar: string }`, there's a whole world of type-level programming that can make your backend code safer and more expressive. Let's explore it.

## Discriminated Unions for Type-Safe State

Model states that can't coexist using discriminated unions:

```typescript
// Instead of nullable fields (confusing)
interface Order {
    id: string;
    status: string;
    paidAt?: Date;        // Only when paid
    trackingCode?: string; // Only when shipped
    cancelReason?: string; // Only when cancelled
}

// Use discriminated unions (explicit)
type Order =
  | { status: 'pending';   id: string; createdAt: Date }
  | { status: 'paid';      id: string; createdAt: Date; paidAt: Date; amount: number }
  | { status: 'shipped';   id: string; createdAt: Date; paidAt: Date; trackingCode: string }
  | { status: 'cancelled'; id: string; createdAt: Date; cancelReason: string };

function processOrder(order: Order) {
    switch (order.status) {
        case 'paid':
            // TypeScript knows paidAt and amount exist here
            console.log(`Paid ${order.amount} at ${order.paidAt}`);
            break;
        case 'shipped':
            // TypeScript knows trackingCode exists here
            console.log(`Tracking: ${order.trackingCode}`);
            break;
    }
}
```

## Type Guards for Runtime Safety

```typescript
// Type guard: narrows type based on runtime check
function isApiError(error: unknown): error is ApiError {
    return (
        typeof error === 'object' &&
        error !== null &&
        'statusCode' in error &&
        'message' in error
    );
}

async function fetchUser(id: string) {
    try {
        return await api.getUser(id);
    } catch (error: unknown) {
        if (isApiError(error)) {
            // TypeScript knows error.statusCode and error.message exist
            if (error.statusCode === 404) {
                return null;
            }
            throw new Error(`API error ${error.statusCode}: ${error.message}`);
        }
        throw error; // Re-throw unknown errors
    }
}
```

## Utility Types You Should Know

```typescript
// Partial — all fields optional
type UpdateUserRequest = Partial<User>;

// Required — all fields required (opposite of Partial)
type StrictUser = Required<User>;

// Pick — select specific fields
type UserSummary = Pick<User, 'id' | 'name' | 'email'>;

// Omit — exclude specific fields
type CreateUserRequest = Omit<User, 'id' | 'createdAt'>;

// Record — dictionary type
type UsersByID = Record<string, User>;

// Readonly — prevent mutation
type ImmutableConfig = Readonly<Config>;

// ReturnType — extract function return type
async function getUser(id: string): Promise<User> { ... }
type UserPromise = ReturnType<typeof getUser>; // Promise<User>

// Parameters — extract function parameter types
type GetUserParams = Parameters<typeof getUser>; // [string]

// Extract and Exclude — filter union types
type StringOrNumber = string | number | boolean;
type OnlyStrings = Extract<StringOrNumber, string>;  // string
type NoStrings = Exclude<StringOrNumber, string>;    // number | boolean
```

## Generic Constraints for Reusable Functions

```typescript
// Basic generic
function first<T>(arr: T[]): T | undefined {
    return arr[0];
}

// Constrained generic — T must have an id field
function findById<T extends { id: string }>(items: T[], id: string): T | undefined {
    return items.find(item => item.id === id);
}

// Multiple constraints
function merge<T extends object, U extends object>(target: T, source: U): T & U {
    return { ...target, ...source };
}

// Conditional types
type NonNullable<T> = T extends null | undefined ? never : T;
type Awaited<T> = T extends Promise<infer U> ? U : T;

// Infer keyword — extract types from other types
type UnpackPromise<T> = T extends Promise<infer Inner> ? Inner : T;
type UnpackArray<T> = T extends (infer Element)[] ? Element : T;

// Usage
type UserData = UnpackPromise<ReturnType<typeof fetchUser>>; // User
type OrderItem = UnpackArray<Order['items']>;               // Item
```

## Result Type: Safe Error Handling

Replace try/catch with explicit Result types:

```typescript
type Result<T, E = Error> =
  | { success: true; data: T }
  | { success: false; error: E };

async function getUser(id: string): Promise<Result<User, 'NOT_FOUND' | 'DB_ERROR'>> {
    try {
        const user = await db.users.findById(id);
        if (!user) {
            return { success: false, error: 'NOT_FOUND' };
        }
        return { success: true, data: user };
    } catch {
        return { success: false, error: 'DB_ERROR' };
    }
}

// Caller handles both cases explicitly
const result = await getUser('42');
if (!result.success) {
    switch (result.error) {
        case 'NOT_FOUND':
            return res.status(404).json({ error: 'User not found' });
        case 'DB_ERROR':
            return res.status(500).json({ error: 'Database error' });
    }
}
// TypeScript knows result.data is User here
const user = result.data;
```

## Zod for Runtime Validation

TypeScript types are erased at runtime. Use Zod to validate external data:

```typescript
import { z } from 'zod';

const CreateOrderSchema = z.object({
    userId: z.string().uuid(),
    items: z.array(z.object({
        productId: z.string().uuid(),
        quantity: z.number().int().positive().max(100),
    })).min(1),
    shippingAddress: z.object({
        street: z.string().min(1),
        city: z.string().min(1),
        country: z.string().length(2), // ISO country code
    }),
});

// Infer TypeScript type from schema (single source of truth!)
type CreateOrderRequest = z.infer<typeof CreateOrderSchema>;

// Validate at API boundary
app.post('/orders', async (req, res) => {
    const result = CreateOrderSchema.safeParse(req.body);

    if (!result.success) {
        return res.status(422).json({
            error: 'Validation failed',
            details: result.error.flatten(),
        });
    }

    // result.data is typed as CreateOrderRequest
    const order = await createOrder(result.data);
    res.status(201).json(order);
});
```

## Template Literal Types

```typescript
// Type-safe event names
type EventName = `${string}Created` | `${string}Updated` | `${string}Deleted`;

// Type-safe HTTP methods
type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
type ApiRoute = `${HttpMethod} /api/${string}`;

// Type-safe environment variable keys
type EnvKey = `DATABASE_${string}` | `REDIS_${string}` | `JWT_${string}`;

function getEnv(key: EnvKey): string {
    const value = process.env[key];
    if (!value) throw new Error(`Missing env var: ${key}`);
    return value;
}

getEnv('DATABASE_URL');  // OK
getEnv('DB_URL');         // TypeScript error!
```

TypeScript's type system is Turing-complete. You can encode complex business rules at the type level, making impossible states unrepresentable and catching entire categories of bugs before they reach production.
