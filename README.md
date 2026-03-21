# WARROOM — Rehearse Your Outage in Plain English

WARROOM is a local-first AI resilience drill simulator for containerized apps. A user types an outage fear in plain English, approves a blast radius, watches a controlled failure unfold, and receives an evidence-backed verdict using real metrics and logs.

## Current Status

### What's Built (Member B — Demo Environment)
- Flask checkout demo app with real Postgres database
- Toxiproxy for latency injection between app and database
- Full compose setup — single command starts the entire stack
- Tested and confirmed: DB Down drill works (stopping DB causes app to return 500 errors, restarting recovers)

### What's Pending (Member A — Core)
- Frontend (4 screens: Fear Input, Approval, Battle, Verdict)
- Backend (FastAPI with 5 API routes)
- MCP Server (FastMCP with 3 tools)
- LLM Integration (classification + verdict summarization)

## Quick Start

### Prerequisites
- Podman Desktop installed with Podman machine running (or Docker)
- Works on both Windows and macOS

### Start the full stack
```
podman compose up
```
Or with Docker:
```
docker compose up
```

First run will build the demo app image and pull Postgres, Toxiproxy, and curl images. Subsequent runs use cached images.

### Verify it's working

Health check:
```
curl http://localhost:5000/health
```
Expected: `{"status": "healthy", "db": "connected"}`

Checkout:
```
curl -X POST http://localhost:5000/checkout -H "Content-Type: application/json" -d '{}'
```
Expected: `{"status": "success", "response_time": <ms>}`

### Stop everything
```
podman compose down
```

## Architecture

```
Web UI (Member A)
  ↓
FastAPI Backend (Member A)
  ├── calls Local LLM
  └── calls FastMCP
         ├── Podman CLI control
         ├── Toxiproxy commands
         └── hey load test
                 ↓
         Demo Environment (Member B) ← THIS IS WHAT'S BUILT
         ├── warroom-app (Flask on port 5000)
         ├── warroom-db (Postgres on port 5432)
         └── warroom-toxiproxy (proxy on port 5433, API on port 8474)
```

## Services (Locked Names — Do Not Change)

| Service | Image | Port | Purpose |
|---|---|---|---|
| warroom-app | Built from ./demo-app | 5000 | Flask checkout demo app |
| warroom-db | postgres:16 | 5432 (internal) | Postgres database |
| warroom-toxiproxy | shopify/toxiproxy:2.12.0 | 8474 (API), 5433 (proxy) | Latency injection proxy |
| warroom-toxiproxy-setup | curlimages/curl | — | One-shot proxy creation |
| warroom-backend | placeholder | 8000 | Member A replaces this |
| warroom-mcp | placeholder | — | Member A replaces this |

## API Contracts (Locked — Do Not Change)

### POST /checkout
Request: `{}` (or with optional fields: item, quantity, total)

Success (200):
```json
{"status": "success", "response_time": 5}
```

Failure (500):
```json
{"status": "error", "response_time": 0, "error": "connection to server failed..."}
```

### GET /health
Healthy (200):
```json
{"status": "healthy", "db": "connected"}
```

Unhealthy (503):
```json
{"status": "unhealthy", "db": "disconnected"}
```

## How Drills Work Against This Environment

### Drill 1: DB Down
```
podman stop warroom_warroom-db_1
```
Effect: /checkout returns 500, /health returns 503

Recovery:
```
podman start warroom_warroom-db_1
```

### Drill 2: Latency Spike
```
curl -X POST http://localhost:8474/proxies/warroom-db-proxy/toxics -H "Content-Type: application/json" -d '{"name":"latency","type":"latency","attributes":{"latency":800}}'
```
Effect: /checkout response_time increases significantly

Remove latency:
```
curl -X DELETE http://localhost:8474/proxies/warroom-db-proxy/toxics/latency
```

### Drill 3: Request Flood
```
hey -c 30 -z 20s -m POST -H "Content-Type: application/json" -d '{}' http://localhost:5000/checkout
```
Effect: High concurrent load, potential latency increase and errors

## Database Schema

Table: `checkouts`
| Column | Type | Notes |
|---|---|---|
| id | SERIAL | Primary key, auto-increment |
| item | TEXT | Default: "demo-item" |
| quantity | INTEGER | Default: 1 |
| total | DOUBLE PRECISION | Default: 9.99 |
| created_at | TIMESTAMP | Auto-set to NOW() |

Seeded with 5 demo rows on first startup.

## App Behavior Notes
- The Flask app connects to Postgres through Toxiproxy (warroom-toxiproxy:5433), NOT directly to warroom-db:5432. This is required for the latency drill to work.
- On startup, the app retries the database connection up to 10 times with 2-second intervals. This handles compose startup ordering.
- Every checkout request is logged to stdout: `[TIMESTAMP] STATUS_CODE /checkout RESPONSE_TIME_MS`

## File Structure
```
warroom/
├── README.md
├── compose.yaml
├── demo-app/
│   ├── Containerfile
│   ├── app.py
│   └── requirements.txt
```

## For Member A: Integration Guide

When building the MCP server tools:

1. **run_drill("db_down")** → run `podman stop warroom_warroom-db_1`
2. **run_drill("latency_spike")** → POST toxic to `http://localhost:8474/proxies/warroom-db-proxy/toxics`
3. **run_drill("request_flood")** → run `hey` against `http://warroom-app:5000/checkout` (use internal network name, or localhost:5000 from host)
4. **reset()** → restart DB, remove toxics, verify /health returns healthy
5. **get_evidence(drill_id)** → poll /checkout during drill, collect response codes and times

Container names follow the pattern: `warroom_<service-name>_1`. Run `podman ps` to confirm exact names on your machine.

Replace the placeholder services in compose.yaml:
- `warroom-backend`: your FastAPI server (port 8000)
- `warroom-mcp`: your FastMCP server
