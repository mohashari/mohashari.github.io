---
layout: post
title: "Go Concurrency Patterns: Goroutines, Channels, and Beyond"
tags: [golang, concurrency, backend]
description: "Master Go's concurrency model — goroutines, channels, sync primitives, and production-ready patterns for building concurrent systems."
---

Go's concurrency model is one of its greatest strengths. Goroutines are cheap, channels make communication clean, and the standard library gives you everything you need. But getting it right requires understanding the patterns. Let's dig in.

## Goroutines: Lightweight Threads

A goroutine is a function running concurrently with other goroutines in the same address space. They start with ~8KB of stack and grow as needed.

```go
func main() {
    // Start 10,000 goroutines with ease
    for i := 0; i < 10000; i++ {
        go func(id int) {
            time.Sleep(time.Second)
            fmt.Printf("Worker %d done\n", id)
        }(i)
    }
    time.Sleep(2 * time.Second)
}
```

In other languages, 10,000 threads would exhaust memory. In Go, this is fine.

## Channels: Communication Between Goroutines

> "Do not communicate by sharing memory; instead, share memory by communicating." — Go Proverb

```go
// Buffered channel
ch := make(chan int, 10)  // Buffer of 10

// Producer
go func() {
    for i := 0; i < 5; i++ {
        ch <- i  // Send
    }
    close(ch)  // Signal done
}()

// Consumer
for val := range ch {  // Receive until closed
    fmt.Println(val)
}
```

## Pattern 1: Worker Pool

Limit concurrency to avoid overwhelming downstream systems:

```go
func WorkerPool(jobs <-chan Job, numWorkers int) <-chan Result {
    results := make(chan Result, len(jobs))

    var wg sync.WaitGroup
    for i := 0; i < numWorkers; i++ {
        wg.Add(1)
        go func() {
            defer wg.Done()
            for job := range jobs {
                result := processJob(job)
                results <- result
            }
        }()
    }

    go func() {
        wg.Wait()
        close(results)
    }()

    return results
}

// Usage
jobs := make(chan Job, 100)
results := WorkerPool(jobs, 10)  // 10 concurrent workers

// Feed jobs
go func() {
    for _, j := range myJobs {
        jobs <- j
    }
    close(jobs)
}()

// Collect results
for r := range results {
    handleResult(r)
}
```

## Pattern 2: Fan-Out, Fan-In

Distribute work across multiple goroutines, then merge results:

```go
func fanOut(input <-chan Work, n int) []<-chan Result {
    channels := make([]<-chan Result, n)
    for i := 0; i < n; i++ {
        channels[i] = worker(input)
    }
    return channels
}

func fanIn(channels ...<-chan Result) <-chan Result {
    merged := make(chan Result)
    var wg sync.WaitGroup

    for _, ch := range channels {
        wg.Add(1)
        go func(c <-chan Result) {
            defer wg.Done()
            for result := range c {
                merged <- result
            }
        }(ch)
    }

    go func() {
        wg.Wait()
        close(merged)
    }()

    return merged
}
```

## Pattern 3: Context for Cancellation

Always propagate context for cancellation and deadlines:

```go
func fetchUserData(ctx context.Context, userID string) (*UserData, error) {
    ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
    defer cancel()

    // All downstream calls get the context
    user, err := userRepo.Get(ctx, userID)
    if err != nil {
        return nil, err
    }

    orders, err := orderRepo.GetByUser(ctx, userID)
    if err != nil {
        return nil, err
    }

    return &UserData{User: user, Orders: orders}, nil
}

// HTTP handler passes context from request
func handler(w http.ResponseWriter, r *http.Request) {
    data, err := fetchUserData(r.Context(), r.URL.Query().Get("id"))
    if err != nil {
        if errors.Is(err, context.DeadlineExceeded) {
            http.Error(w, "Timeout", http.StatusGatewayTimeout)
            return
        }
        http.Error(w, "Error", http.StatusInternalServerError)
        return
    }
    json.NewEncoder(w).Encode(data)
}
```

## Pattern 4: errgroup for Parallel Operations

Run multiple operations concurrently and collect errors:

```go
import "golang.org/x/sync/errgroup"

func loadDashboard(ctx context.Context, userID string) (*Dashboard, error) {
    g, ctx := errgroup.WithContext(ctx)

    var user *User
    var orders []Order
    var notifications []Notification

    g.Go(func() error {
        var err error
        user, err = userRepo.Get(ctx, userID)
        return err
    })

    g.Go(func() error {
        var err error
        orders, err = orderRepo.GetRecent(ctx, userID, 10)
        return err
    })

    g.Go(func() error {
        var err error
        notifications, err = notifRepo.GetUnread(ctx, userID)
        return err
    })

    if err := g.Wait(); err != nil {
        return nil, err
    }

    return &Dashboard{
        User:          user,
        RecentOrders:  orders,
        Notifications: notifications,
    }, nil
}
```

This runs all three queries in parallel, cutting 3 sequential queries (e.g., 3 × 50ms = 150ms) down to max(50ms, 50ms, 50ms) = ~50ms.

## sync.Mutex vs sync.RWMutex

```go
type SafeMap struct {
    mu sync.RWMutex
    m  map[string]string
}

func (sm *SafeMap) Get(key string) (string, bool) {
    sm.mu.RLock()   // Multiple readers allowed simultaneously
    defer sm.mu.RUnlock()
    val, ok := sm.m[key]
    return val, ok
}

func (sm *SafeMap) Set(key, val string) {
    sm.mu.Lock()    // Exclusive write lock
    defer sm.mu.Unlock()
    sm.m[key] = val
}
```

Use `sync.RWMutex` when reads are much more frequent than writes.

## sync.Once for Initialization

```go
var (
    dbInstance *sql.DB
    once       sync.Once
)

func GetDB() *sql.DB {
    once.Do(func() {
        // This runs exactly once, even across goroutines
        db, err := sql.Open("postgres", os.Getenv("DATABASE_URL"))
        if err != nil {
            log.Fatal(err)
        }
        dbInstance = db
    })
    return dbInstance
}
```

## Common Mistakes

### Goroutine Leak

Always ensure goroutines can exit:

```go
// WRONG: goroutine leaks if nobody reads from ch
go func() {
    result := doExpensiveWork()
    ch <- result  // Blocks forever if receiver is gone
}()

// RIGHT: use context for cancellation
go func() {
    select {
    case ch <- doExpensiveWork():
    case <-ctx.Done():
        return  // Goroutine exits cleanly
    }
}()
```

### Data Race

Use `go test -race` to detect data races:

```bash
go test -race ./...
```

### Closing a Channel Twice

```go
// WRONG: panics if close is called twice
close(ch)
close(ch)  // panic: close of closed channel

// RIGHT: use sync.Once
var once sync.Once
safeClose := func() { once.Do(func() { close(ch) }) }
```

Go's concurrency model is powerful but requires discipline. Use `-race` in tests, always propagate context, and design goroutine lifecycles explicitly.
