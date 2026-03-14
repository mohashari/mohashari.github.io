---
layout: post
title: "gRPC vs REST: The Complete Comparison for Backend Engineers"
tags: [grpc, rest, api, backend, performance]
description: "A deep-dive comparison of gRPC and REST — performance, tooling, use cases, and when to pick each."
---

gRPC has been gaining serious traction in the microservices world. Built on HTTP/2 and Protocol Buffers, it promises better performance and stronger contracts than REST. But should you switch? Let's compare honestly.

![gRPC vs REST Communication](/images/diagrams/grpc-vs-rest.svg)

## What is gRPC?

gRPC (Google Remote Procedure Call) uses:
- **HTTP/2** — multiplexing, header compression, bidirectional streaming
- **Protocol Buffers** — binary serialization format (smaller, faster than JSON)
- **Code generation** — client and server stubs generated from `.proto` files

```protobuf
// user.proto
syntax = "proto3";

package user;

service UserService {
  rpc GetUser(GetUserRequest) returns (User);
  rpc ListUsers(ListUsersRequest) returns (stream User);
  rpc CreateUser(CreateUserRequest) returns (User);
}

message GetUserRequest {
  string user_id = 1;
}

message User {
  string id = 1;
  string name = 2;
  string email = 3;
  int64 created_at = 4;
}
```

Generate code:
```bash
protoc --go_out=. --go-grpc_out=. user.proto
```

You get type-safe client and server code in any supported language.

## Performance: gRPC Wins Clearly

| Metric | REST/JSON | gRPC/Protobuf |
|--------|-----------|---------------|
| Payload size | ~baseline | 40-60% smaller |
| Serialization speed | ~baseline | 5-10x faster |
| HTTP version | HTTP/1.1 | HTTP/2 |
| Multiplexing | No (1 req/connection) | Yes |
| Header compression | No | Yes (HPACK) |

For high-throughput service-to-service communication, these differences are significant.

## The Streaming Advantage

gRPC has four communication patterns:

### 1. Unary (same as REST)
```
Client → Request → Server
Client ← Response ← Server
```

### 2. Server Streaming
```
Client → Request → Server
Client ← Response ←
Client ← Response ←
Client ← Response ← Server (stream)
```

```go
// Server streaming example
func (s *server) ListUsers(req *pb.ListUsersRequest, stream pb.UserService_ListUsersServer) error {
    rows, err := db.QueryUsers(req.Filter)
    for rows.Next() {
        user := scanUser(rows)
        if err := stream.Send(user); err != nil {
            return err
        }
    }
    return nil
}
```

### 3. Client Streaming

Client sends a stream of messages; server responds once. Good for bulk uploads.

### 4. Bidirectional Streaming

Both sides stream simultaneously. Great for chat, real-time collaboration.

```go
func (s *server) Chat(stream pb.ChatService_ChatServer) error {
    for {
        msg, err := stream.Recv()
        if err == io.EOF {
            return nil
        }
        // Broadcast to other users...
        stream.Send(&pb.ChatMessage{Content: "echoed: " + msg.Content})
    }
}
```

## Developer Experience: REST Wins

gRPC has real friction points:

### Browser Support is Limited

gRPC requires HTTP/2 trailers which browsers don't natively support. You need **gRPC-Web** with an Envoy/NGINX proxy, which adds complexity.

### Debugging is Harder

With REST, you `curl` and see JSON. With gRPC, you need tools like `grpcurl` or `Postman gRPC`:

```bash
# Inspect available services
grpcurl -plaintext localhost:50051 list

# Call a method
grpcurl -plaintext -d '{"user_id": "42"}' \
  localhost:50051 user.UserService/GetUser
```

### Less Universal Tooling

REST has Swagger/OpenAPI, Postman, thousands of tutorials. gRPC tooling is catching up but isn't there yet.

## Go Implementation Example

```go
// Server
func main() {
    lis, err := net.Listen("tcp", ":50051")
    if err != nil {
        log.Fatalf("failed to listen: %v", err)
    }

    s := grpc.NewServer(
        grpc.UnaryInterceptor(authInterceptor),
        grpc.MaxRecvMsgSize(1024*1024*4),
    )

    pb.RegisterUserServiceServer(s, &UserServiceServer{db: db})
    reflection.Register(s)  // Enable grpcurl inspection

    log.Printf("gRPC server listening on :50051")
    s.Serve(lis)
}

// Client
func main() {
    conn, err := grpc.Dial("localhost:50051",
        grpc.WithTransportCredentials(insecure.NewCredentials()),
    )
    defer conn.Close()

    client := pb.NewUserServiceClient(conn)

    ctx, cancel := context.WithTimeout(context.Background(), time.Second)
    defer cancel()

    user, err := client.GetUser(ctx, &pb.GetUserRequest{UserId: "42"})
    fmt.Printf("Got user: %v\n", user)
}
```

## When to Use gRPC

**Choose gRPC when:**
- Internal service-to-service communication (microservices)
- Performance is critical and payload size matters
- You need streaming (server, client, or bidirectional)
- Polyglot environment (auto-generated clients in Go, Java, Python, etc.)
- Strong contract enforcement is a priority

**Choose REST when:**
- Public-facing APIs (browser/mobile clients)
- Team unfamiliarity with gRPC
- You need human-readable payloads for debugging
- Simple CRUD without complex streaming needs
- Third-party integration (most support REST)

## The Hybrid Approach

Many organizations use both:

```
Browser/Mobile → REST (via API Gateway) → Internal Services via gRPC
```

The API Gateway translates HTTP/JSON to gRPC internally. You get the ergonomics of REST externally and the performance of gRPC internally. This is the approach Google, Netflix, and many large-scale companies use.

## Bottom Line

gRPC is genuinely better for internal microservice communication — better performance, stronger contracts, great streaming support. REST remains the right choice for public APIs. Pick based on your actual use case, not trend-chasing.
