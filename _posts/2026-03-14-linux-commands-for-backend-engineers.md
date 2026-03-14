---
layout: post
title: "Linux Commands Every Backend Engineer Must Know"
tags: [linux, devops, backend, tools]
description: "The essential Linux commands for backend engineers — process management, networking, file operations, and debugging in production."
---

Production issues don't wait for business hours. When you're SSH'd into a server at 2am during an incident, you need these commands by muscle memory. Here's the survival guide.

## Process Management

```bash
# Find what's running on a port
ss -tlnp | grep :8080
lsof -i :8080

# Kill a process on a port
kill $(lsof -t -i:8080)

# List processes by CPU/memory
ps aux --sort=-%cpu | head -20
ps aux --sort=-%mem | head -20

# Real-time process monitor
top         # Basic
htop        # Better (install with: apt install htop)
glances     # Even better

# Run a process and keep it after logout
nohup ./myapp &
disown %1

# Background process management
jobs        # List background jobs
fg %1       # Bring job 1 to foreground
bg %1       # Continue job 1 in background

# System resource usage
free -h     # Memory
df -h       # Disk space
df -h /     # Disk space for root
du -sh *    # Directory sizes in current folder
du -sh /* 2>/dev/null | sort -h  # Find largest directories
```

## File Operations

```bash
# Find files
find /var/log -name "*.log" -newer /tmp/marker
find . -name "*.go" -size +1M
find . -mtime -1  # Modified in last 24 hours

# Search inside files
grep -r "ERROR" /var/log/myapp/
grep -r "panic" . --include="*.go"
grep -n "func.*Handler" src/api/*.go

# Real-time log tailing
tail -f /var/log/app.log
tail -f /var/log/app.log | grep ERROR

# Watch multiple log files
multitail /var/log/app.log /var/log/nginx/access.log

# Count occurrences
grep -c "ERROR" /var/log/app.log
grep "ERROR" /var/log/app.log | wc -l

# Process log files
cat access.log | awk '{print $1}' | sort | uniq -c | sort -rn | head -20
# → Top 20 IPs by request count

# Extract fields from JSON logs
cat app.log | jq '.level + " " + .message' | grep ERROR
```

## Networking

```bash
# Check connectivity
ping -c 4 google.com
traceroute google.com
mtr google.com  # Combined ping + traceroute

# DNS lookup
dig api.example.com
dig api.example.com @8.8.8.8  # Use specific DNS server
nslookup api.example.com

# Check open ports
ss -tlnp        # Listening TCP ports
ss -tunap       # All connections
netstat -tlnp   # Alternative (older)

# Test HTTP endpoints
curl -v https://api.example.com/health
curl -w "\nTime: %{time_total}s\nHTTP: %{http_code}\n" -o /dev/null https://api.example.com

# Test with timeout
curl --connect-timeout 5 --max-time 10 https://api.example.com/health

# Download files
wget https://example.com/file.tar.gz
curl -O https://example.com/file.tar.gz

# Check bandwidth
iftop -n    # Real-time bandwidth by connection
nethogs     # Per-process bandwidth

# TCP dump (network capture)
tcpdump -i eth0 port 8080 -A
tcpdump -i eth0 host 192.168.1.100
```

## System Performance Debugging

```bash
# CPU profiling
iostat -x 1        # I/O stats every second
vmstat 1           # Virtual memory stats
sar -u 1 10        # CPU usage (10 samples)

# Memory
free -h
cat /proc/meminfo
smem -t -k         # Per-process memory

# Disk I/O
iostat -xz 1
iotop              # Per-process I/O (like top for disk)
dstat              # Combined stats

# Load average explained
uptime             # Shows 1m, 5m, 15m load averages
# Load = 1.0 → 100% CPU utilization on single-core
# On 4-core: load = 4.0 means fully utilized
# Rule of thumb: load > num_cores = you have a problem

# File descriptors
ulimit -n          # Current fd limit
cat /proc/sys/fs/file-max  # System-wide limit
lsof | wc -l      # Current open files
```

## SSH and Remote Access

```bash
# SSH with key
ssh -i ~/.ssh/mykey.pem ubuntu@192.168.1.100

# SSH config (~/.ssh/config)
Host myserver
    HostName 192.168.1.100
    User ubuntu
    IdentityFile ~/.ssh/mykey.pem
    ServerAliveInterval 60

# Now just: ssh myserver

# Copy files
scp file.txt ubuntu@server:/home/ubuntu/
rsync -avz ./local/ ubuntu@server:/remote/  # Better than scp for directories

# Port forwarding (access remote service locally)
ssh -L 5432:localhost:5432 ubuntu@server  # Access remote PostgreSQL locally
# Then: psql -h localhost -p 5432

# Reverse tunnel (expose local service to remote)
ssh -R 8080:localhost:8080 ubuntu@server

# SSH multiplexing (reuse connections)
# Add to ~/.ssh/config:
Host *
    ControlMaster auto
    ControlPath ~/.ssh/control/%h_%p_%r
    ControlPersist 600
```

## System Logs

```bash
# systemd journal
journalctl -u myapp              # Service logs
journalctl -u myapp -f           # Follow
journalctl -u myapp --since "1 hour ago"
journalctl -p err -u myapp       # Error level only
journalctl --disk-usage          # Journal disk usage

# Traditional logs
tail -f /var/log/syslog
tail -f /var/log/auth.log        # SSH login attempts

# Check service status
systemctl status myapp
systemctl restart myapp
systemctl enable myapp  # Start on boot
```

## Quick Production Cheatsheet

```bash
# Something's using too much CPU?
ps aux --sort=-%cpu | head -5

# Port already in use?
ss -tlnp | grep :8080
kill $(lsof -t -i:8080)

# Disk full?
df -h
du -sh /var/* | sort -h | tail -10

# Too many connections?
ss -s        # Summary of connections
ss -tn state established | wc -l

# What's the app doing right now?
strace -p <pid>           # System calls
lsof -p <pid>             # Open files/sockets

# Memory leak suspected?
watch -n 2 'ps aux --sort=-%mem | head -5'

# Quick load test
ab -n 1000 -c 50 http://localhost:8080/api/health
```

These commands will get you through 90% of production incidents. Practice them in a safe environment until they become second nature — because under pressure, you want muscle memory, not googling.
