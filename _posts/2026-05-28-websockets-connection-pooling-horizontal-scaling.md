---
layout: post
title: "Advanced WebSockets: Connection Pooling, Backpressure, and Horizontal Scaling"
date: 2026-05-28 08:00:00 +0700
tags: [websockets, scalability, backend, concurrency, system-design]
description: "Strategies for scaling WebSocket connections to hundreds of thousands of concurrent users using Redis Pub/Sub, connection pools, and load balancers."
image: ""
thumbnail: ""
---

Building real-time features like chat applications, collaborative dashboards, or live trading feeds requires a bi-directional communication protocol. **WebSockets** (standardized in RFC 6455) are the industry standard, allowing servers to push data to clients instantly without the overhead of HTTP polling.

However, moving from HTTP to WebSockets completely changes your backend's resource profile. Unlike HTTP requests—which are stateless, transactional, and short-lived—**WebSocket connections are stateful, persistent TCP sockets.**

Scaling a stateful protocol horizontally presents massive system challenges. In this advanced guide, we'll explore **horizontal routing via Redis Pub/Sub**, **operating system socket tuning**, and handling **slow client backpressure** to scale WebSockets to hundreds of thousands of concurrent connections.

---

## 1. The Horizontal Scaling Problem: Cross-Server Routing

When you scale your application horizontally by adding more servers behind a load balancer, each server holds a distinct subset of active client connections.

```
                  ┌─────────────────┐
                  │  Load Balancer  │
                  └────────┬────────┘
             ┌─────────────┴─────────────┐
             ▼                           ▼
      ┌──────────────┐            ┌──────────────┐
      │   Server 1   │            │   Server 2   │
      │  [Client A]  │            │  [Client B]  │
      └──────────────┘            └──────────────┘
```

If **Client A** (on Server 1) wants to send a direct message to **Client B** (on Server 2), Server 1 cannot deliver it directly. The TCP socket for Client B physically resides in Server 2's RAM.

### The Solution: A Redis Pub/Sub Message Bus

To bridge the gap between servers, implement a shared high-speed message bus. Each server subscribes to a global Redis channel (or a channel per user ID).

```
Client A ──► Server 1 ──► [Redis Channel] ──► Server 2 ──► Client B
```

1.  When **Client B** connects to **Server 2**, Server 2 subscribes to a Redis channel named `user:client-b`.
2.  When **Client A** sends a message to Client B, **Server 1** intercepts it and publishes the payload to the `user:client-b` Redis channel.
3.  Redis instantly broadcasts the message to **Server 2**.
4.  **Server 2** receives the event and writes it to Client B's physical WebSocket connection.

---

## 2. Tuning the Operating System for High Connection Density

By default, standard Linux operating system limits are configured to protect servers from running out of memory due to runaway processes. To support over 50,000 concurrent sockets per node, you **must** tune these kernel limits.

### A. File Descriptor Limits
In Unix-like systems, "everything is a file." Every active TCP connection consumes one **File Descriptor (FD)**. The default shell limit is often `1024` FDs.

To support massive scale, edit `/etc/security/limits.conf` to increase the maximum open file limits for your application user:

```text
# /etc/security/limits.conf
# snippet-1
appuser    soft    nofile    1048576
appuser    hard    nofile    1048576
```

### B. TCP Window and Buffer Limits
By default, the Linux TCP stack allocates up to 4MB of write/read buffer space per socket. While this maximizes throughput for single downloads, 100,000 idle WebSockets would consume 400GB of RAM just for buffers!

Configure the kernel to allow TCP buffers to scale down aggressively for quiet connections by modifying `/etc/sysctl.conf`:

```text
# /etc/sysctl.conf
# snippet-2
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
```

Apply the changes immediately:
```bash
sudo sysctl -p
```

---

## 3. The Silent Killer: Slow Client Backpressure

In a production environment, users connect over volatile mobile networks. If your server generates messages faster than a client's network can download them, the **outgoing TCP write buffer fills up**.

If the server continues to enqueue messages without blocking, those messages pile up in the server's user-space memory, eventually triggering an **OOM (Out Of Memory) crash**.

### The Solution: Read/Write Pumps with Timeouts

To prevent memory bloat, design your WebSocket handlers using separate **read** and **write** loops (pumps) connected by buffered Go channels, and enforce strict write deadlines.

Here is a robust Go implementation utilizing the `gorilla/websocket` library:

```go
// snippet-3
package main

import (
	"log"
	"net/http"
	"time"

	"github.com/gorilla/websocket"
)

const (
	writeWait      = 10 * time.Second    // Time allowed to write a message to the peer.
	pongWait       = 60 * time.Second    // Time allowed to read the next pong message from the peer.
	pingPeriod     = (pongWait * 9) / 10 // Send pings to peer with this period. Must be less than pongWait.
	maxMessageSize = 512                 // Maximum message size allowed from peer.
)

var upgrader = websocket.Upgrader{
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
	CheckOrigin: func(r *http.Request) bool {
		return true // Validate origin in production!
	},
}

type Client struct {
	hub  *Hub
	conn *websocket.Conn
	send chan []byte // Buffered channel of outbound messages
}

// writePump handles writing outbound messages to the physical TCP socket
func (c *Client) writePump() {
	ticker := time.NewTicker(pingPeriod)
	defer func() {
		ticker.Stop()
		c.conn.Close()
	}()

	for {
		select {
		case message, ok := <-c.send:
			// Set a write deadline. If the client's network is too slow,
			// this write will timeout and close the connection, freeing memory.
			_ = c.conn.SetWriteDeadline(time.Now().Add(writeWait))
			if !ok {
				_ = c.conn.WriteMessage(websocket.CloseMessage, []byte{})
				return
			}

			w, err := c.conn.NextWriter(websocket.TextMessage)
			if err != nil {
				return
			}
			_, _ = w.Write(message)

			if err := w.Close(); err != nil {
				return
			}

		case <-ticker.C:
			_ = c.conn.SetWriteDeadline(time.Now().Add(writeWait))
			if err := c.conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}

// readPump handles reading inbound messages from the physical TCP socket
func (c *Client) readPump() {
	defer func() {
		c.hub.unregister <- c
		c.conn.Close()
	}()

	c.conn.SetReadLimit(maxMessageSize)
	_ = c.conn.SetReadDeadline(time.Now().Add(pongWait))
	c.conn.SetPongHandler(func(string) error {
		_ = c.conn.SetReadDeadline(time.Now().Add(pongWait))
		return nil
	})

	for {
		_, _, err := c.conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseAbnormalClosure) {
				log.Printf("error: %v", err)
			}
			break
		}
		// Process incoming message or route to hub
	}
}
```

By enforcing `SetWriteDeadline` on the socket, any slow client that falls too far behind will trigger a write timeout, causing the connection to disconnect and releasing its allocated buffers before memory is exhausted.

---

## Summary

Scaling real-time systems horizontally requires shifting from a request-centric to a socket-centric design mindset. By combining distributed routing buses like Redis Pub/Sub, tuning Linux file descriptor limits, and protecting server memory using strict write deadlines, you can easily handle millions of real-time events across your global backend cluster.
