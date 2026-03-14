---
layout: post
title: "Clean Code Principles That Make Your Team Love You"
tags: [clean-code, best-practices, engineering]
description: "Practical clean code principles with real before/after examples that make code easier to read, maintain, and extend."
---

Code is written once but read hundreds of times. Clean code isn't about aesthetics — it's about reducing the cognitive load of everyone who reads it, including future you. Here are the principles that actually matter.

## 1. Names Should Reveal Intent

The name of a variable, function, or class should tell you why it exists, what it does, and how it's used.

```go
// Bad
func calc(d []int, t int) int {
    r := 0
    for _, v := range d {
        if v > t {
            r++
        }
    }
    return r
}

// Good
func countValuesAboveThreshold(values []int, threshold int) int {
    count := 0
    for _, value := range values {
        if value > threshold {
            count++
        }
    }
    return count
}
```

If you need a comment to explain a variable name, rename the variable.

## 2. Functions Should Do One Thing

A function that does multiple things is harder to test, name, and reason about.

```go
// Bad: This function does 3 things
func processUser(userID string) error {
    // 1. Validate
    if userID == "" {
        return errors.New("user ID required")
    }

    // 2. Fetch from DB
    user, err := db.GetUser(userID)
    if err != nil {
        return err
    }

    // 3. Send welcome email
    return emailService.SendWelcome(user.Email, user.Name)
}

// Good: Each function does one thing
func validateUserID(userID string) error {
    if userID == "" {
        return errors.New("user ID required")
    }
    return nil
}

func sendWelcomeEmail(ctx context.Context, userID string) error {
    user, err := userRepo.Get(ctx, userID)
    if err != nil {
        return fmt.Errorf("fetch user: %w", err)
    }
    return emailService.SendWelcome(ctx, user.Email, user.Name)
}
```

## 3. Keep Functions Small

If a function is more than ~20 lines, it's probably doing too much. Extract sub-operations into named functions:

```go
// Hard to read
func generateReport(orders []Order) Report {
    var total float64
    var byCategory = make(map[string]float64)
    for _, o := range orders {
        total += o.Amount
        byCategory[o.Category] += o.Amount
    }
    topCategory := ""
    topAmount := 0.0
    for cat, amt := range byCategory {
        if amt > topAmount {
            topAmount = amt
            topCategory = cat
        }
    }
    return Report{Total: total, TopCategory: topCategory}
}

// Easy to read
func generateReport(orders []Order) Report {
    total := sumOrderAmounts(orders)
    byCategory := groupByCategory(orders)
    topCategory := findTopCategory(byCategory)
    return Report{Total: total, TopCategory: topCategory}
}
```

## 4. Avoid Magic Numbers and Strings

```go
// Bad
if retries > 3 {
    sleep(500)
}

// Good
const (
    maxRetries     = 3
    retryDelayMs   = 500
)

if retries > maxRetries {
    sleep(retryDelayMs * time.Millisecond)
}
```

## 5. Error Handling is Not an Afterthought

Handle errors explicitly and at the right level:

```go
// Bad: swallowing errors
func getUser(id string) *User {
    user, _ := db.Find(id)  // Silently ignores error
    return user
}

// Bad: handling errors at wrong level
func saveOrder(order Order) {
    if err := db.Save(order); err != nil {
        log.Printf("error: %v", err)  // Log and continue?!
    }
}

// Good: return errors, let callers decide
func getUser(ctx context.Context, id string) (*User, error) {
    user, err := db.Find(ctx, id)
    if err != nil {
        return nil, fmt.Errorf("get user %s: %w", id, err)
    }
    return user, nil
}
```

## 6. Prefer Explicit Over Implicit

Don't make readers guess what your code does:

```go
// Bad: what does "true" mean here?
createUser("alice@example.com", true, false)

// Good: named parameters (or options pattern)
createUser(CreateUserOptions{
    Email:    "alice@example.com",
    SendWelcomeEmail: true,
    RequireVerification: false,
})
```

## 7. Avoid Deep Nesting — Use Early Returns

```go
// Bad: pyramid of doom
func processOrder(order Order) error {
    if order.UserID != "" {
        if order.Items != nil {
            if len(order.Items) > 0 {
                if order.Total > 0 {
                    // Actual logic buried here
                    return db.Save(order)
                } else {
                    return errors.New("total must be positive")
                }
            } else {
                return errors.New("order must have items")
            }
        } else {
            return errors.New("items required")
        }
    } else {
        return errors.New("user ID required")
    }
}

// Good: guard clauses + early returns
func processOrder(order Order) error {
    if order.UserID == "" {
        return errors.New("user ID required")
    }
    if len(order.Items) == 0 {
        return errors.New("order must have items")
    }
    if order.Total <= 0 {
        return errors.New("total must be positive")
    }

    return db.Save(order)
}
```

## 8. Comments Explain Why, Not What

The code shows **what**. Comments should explain **why**.

```go
// Bad: comment restates the code
// Increment counter by 1
counter++

// Bad: commented-out code
// user.SendEmail()
// user.NotifySlack()

// Good: explains a non-obvious decision
// Use exponential backoff starting at 100ms to avoid thundering herd
// when the upstream service recovers from an outage
delay := 100 * math.Pow(2, float64(attempt)) * time.Millisecond
```

## 9. Write Tests That Document Behavior

Tests are living documentation. Name them to describe behavior:

```go
// Bad
func TestGetUser(t *testing.T) { ... }

// Good
func TestGetUser_ReturnsUser_WhenExists(t *testing.T) { ... }
func TestGetUser_ReturnsNotFoundError_WhenMissing(t *testing.T) { ... }
func TestGetUser_ReturnsError_WhenDatabaseFails(t *testing.T) { ... }
```

## 10. The Boy Scout Rule

> "Leave the code cleaner than you found it."

Every time you touch a file, improve it slightly. Rename a confusing variable, extract a long method, delete dead code. Over time, these small improvements compound dramatically.

Clean code is not written in one pass. It's refined through iteration, code review, and a team culture that values readability over clever tricks.
