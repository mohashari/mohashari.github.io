---
layout: post
title: "Go Design Patterns — Part 5: Singleton"
date: 2026-06-12
tags: [design-patterns, creational, golang, best-practices]
description: "An in-depth guide to implementing the Singleton design pattern in Go (Golang), featuring a database connection pool analogy, sync.Once thread safety, and complete code."
image: "https://picsum.photos/seed/4017/1080/720"
thumbnail: "https://picsum.photos/seed/4017/400/300"
---

The Singleton is a creational design pattern that lets you ensure that a class (or struct) has only one instance, while providing a global access point to this instance. In Go, implementing a Singleton requires careful consideration of concurrency to prevent race conditions during initialization.

## Conceptual Explanation & Real-World Analogy

The Singleton pattern solves two problems at the same time, violating the *Single Responsibility Principle*:
1. **Ensure that a class has just a single instance**: Why would anyone want to control how many instances a class has? The most common reason for this is to control access to some shared resource—for example, a database or a file.
2. **Provide a global access point to that instance**: Remember those global variables that we used to store some essential objects? While they are very handy, they are also very unsafe since any code can potentially overwrite the contents of those variables and crash the app.

Just like the government of a country, a state can have only one official government. Regardless of the personal identities of the individuals who form the government, the title "The Government of X" is a global access point that identifies the group of people in charge.

In software, a Singleton is commonly used for:
- Database connection pools (to avoid exhausting socket connections).
- Configuration managers (to read config files once and share them globally).
- Logging services (to write to a single file from multiple threads).

---

## Conceptual Diagram

Here is a Mermaid class diagram that shows the structure of the Singleton pattern in Go:

```mermaid
classDiagram
    class DatabaseConnection {
        -connectionString string
        +Query(sql string) string
    }

    class SingletonAccessor {
        -instance *DatabaseConnection
        -once sync.Once
        +GetDatabaseConnection() *DatabaseConnection
    }

    SingletonAccessor --> DatabaseConnection : creates & manages
```

---

## Use Case / Problem Scenario

Why do we need this pattern, and why is Go different?
In languages like Java or C++, you can make the constructor of a class private to prevent direct instantiation. Go doesn't have classes, constructors, or access modifiers like `private` or `public`. Instead, we control access using package visibility (capitalization).

Additionally, Go is built for concurrency, using goroutines to execute code in parallel. If two goroutines call your instantiation function at the exact same millisecond, and you haven't implemented thread safety, you will create two separate instances of your "Singleton", defeating the entire purpose.

To solve this in Go, we use the `sync` package. Specifically, `sync.Once` guarantees that a block of code runs exactly once, even if called simultaneously from thousands of goroutines.

---

## Golang Code Example

Below is a complete, compilable Go program demonstrating the Singleton pattern using `sync.Once` to manage a shared database connection pool in a thread-safe manner.

```go
package main

import (
	"fmt"
	"sync"
	"time"
)

// databaseConnection represents our singleton object.
// We keep it lowercase (private to the package) to prevent direct instantiation from other packages.
type databaseConnection struct {
	connectionString string
}

func (db *databaseConnection) Query(sql string) string {
	return fmt.Sprintf("Executing query [%s] on connection [%s]", sql, db.connectionString)
}

var (
	// instance is the single shared object.
	instance *databaseConnection
	// once ensures that initialization is executed only once.
	once sync.Once
)

// GetDatabaseInstance provides the global access point to the singleton instance.
// It is fully thread-safe and utilizes double-checked optimization internally via sync.Once.
func GetDatabaseInstance() *databaseConnection {
	once.Do(func() {
		fmt.Println("Initializing database connection pool... (This should happen only ONCE)")
		// Simulating connection lag
		time.Sleep(100 * time.Millisecond)
		instance = &databaseConnection{
			connectionString: "postgres://user:password@localhost:5432/production_db",
		}
	})
	return instance
}

func main() {
	fmt.Println("Starting Singleton Thread-Safety Test...")

	var wg sync.WaitGroup
	numGoroutines := 10

	// Launch multiple goroutines trying to access the singleton simultaneously
	for i := 1; i <= numGoroutines; i++ {
		wg.Add(1)
		go func(goroutineID int) {
			defer wg.Done()
			
			// Every goroutine calls GetDatabaseInstance
			db := GetDatabaseInstance()
			
			// We print the memory address to verify that all goroutines get the exact same instance
			fmt.Printf("Goroutine %d: Connection pointer: %p\n", goroutineID, db)
		}(i)
	}

	wg.Wait()
	
	// Double-check by calling it one more time from the main goroutine
	finalInstance := GetDatabaseInstance()
	fmt.Printf("\nVerification: Final connection pool pointer: %p\n", finalInstance)
	fmt.Println(finalInstance.Query("SELECT * FROM users;"))
}
```

---

## Summary

### Advantages
- **Single Instance Guarantee**: You can be sure that a class/struct has only a single instance.
- **Global Access**: You gain a global access point to that instance.
- **Lazy Initialization**: The object is initialized only when it is requested for the first time, saving system resources if it's never used.
- **Go-Idiomatic Thread Safety**: By using `sync.Once`, Go handles lock checking efficiently, avoiding slow global locks on subsequent calls.

### Disadvantages
- **Violation of Single Responsibility Principle**: The pattern solves two problems at the same time.
- **Difficult to Unit Test**: Singletons introduce global state, which makes it harder to isolate components during testing. You cannot easily mock a singleton.
- **Hidden Dependencies**: Clients of the singleton can access it anywhere, making dependencies hidden and less explicit in method signatures.

### When to Use
- Use the Singleton pattern when a class/struct in your program must have just a single instance available to all clients; for example, a shared database object or a global configuration store.
- Use the Singleton pattern when you need stricter control over global variables.
