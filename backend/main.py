import subprocess
import time
from typing import Literal

import requests

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


app = FastAPI(title="WARROOM Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ClassifyRequest(BaseModel):
    fear: str


class StartDrillRequest(BaseModel):
    drill_type: Literal["db_down", "latency_spike", "request_flood"]


DRILL_CONFIG = {
    "db_down": {
        "drill_type": "db_down",
        "label": "DB Down",
        "target_service": "warroom-db",
        "duration": "60 seconds",
        "expected_impact": "Checkout requests may return 5xx errors",
    },
    "latency_spike": {
        "drill_type": "latency_spike",
        "label": "Latency Spike",
        "target_service": "warroom-db via toxiproxy",
        "duration": "60 seconds",
        "expected_impact": "Responses may slow down or time out",
    },
    "request_flood": {
        "drill_type": "request_flood",
        "label": "Request Flood",
        "target_service": "warroom-app",
        "duration": "20 seconds",
        "expected_impact": "Success rate may drop under load",
    },
}

DRILL_STATE = {
    "drill_id": None,
    "drill_type": None,
    "status": "idle",
    "poll_count": 0,
    "start_time": None,
    "db_container_name": None,
    "timeline": [],
    "logs": [],
    "latencies_ms": [],
    "success_count": 0,
    "probe_count": 0,
    "error_count": 0,
    "first_failure_time": None,
    "evidence": None,
}


def reset_drill_state() -> None:
    DRILL_STATE["drill_id"] = None
    DRILL_STATE["drill_type"] = None
    DRILL_STATE["status"] = "idle"
    DRILL_STATE["poll_count"] = 0
    DRILL_STATE["start_time"] = None
    DRILL_STATE["db_container_name"] = None
    DRILL_STATE["timeline"] = []
    DRILL_STATE["logs"] = []
    DRILL_STATE["latencies_ms"] = []
    DRILL_STATE["success_count"] = 0
    DRILL_STATE["probe_count"] = 0
    DRILL_STATE["error_count"] = 0
    DRILL_STATE["first_failure_time"] = None
    DRILL_STATE["evidence"] = None


def run_podman_command(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["podman", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def resolve_db_container_name() -> str:
    result = run_podman_command("ps", "-a", "--format", "{{.Names}}")
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Could not inspect Podman containers: {result.stderr.strip()}",
        )

    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    matches = [name for name in names if "warroom-db" in name]

    if not matches:
        raise HTTPException(
            status_code=500,
            detail="Could not find a Podman container matching warroom-db.",
        )

    def score(name: str) -> tuple[int, int]:
        if name == "warroom-db":
            return (0, len(name))
        if name.startswith("warroom-db"):
            return (1, len(name))
        if name.endswith("warroom-db") or name.endswith("_warroom-db_1"):
            return (2, len(name))
        return (3, len(name))

    container_name = sorted(matches, key=score)[0]
    print(f"[WARROOM backend] resolved container name={container_name}")
    return container_name


def container_is_running(container_name: str) -> bool:
    result = run_podman_command("inspect", "-f", "{{.State.Running}}", container_name)
    if result.returncode != 0:
        return False
    return result.stdout.strip().lower() == "true"


def stop_container(container_name: str) -> None:
    result = run_podman_command("stop", container_name)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Could not stop container {container_name}: {result.stderr.strip()}",
        )


def start_container(container_name: str) -> None:
    result = run_podman_command("start", container_name)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Could not start container {container_name}: {result.stderr.strip()}",
        )


def add_timeline_event(event: str) -> None:
    if event not in DRILL_STATE["timeline"]:
        DRILL_STATE["timeline"].append(event)


def append_log(line: str) -> None:
    if line not in DRILL_STATE["logs"]:
        DRILL_STATE["logs"].append(line)


def elapsed_seconds() -> int:
    if not DRILL_STATE["start_time"]:
        return 0
    return max(int(time.time() - DRILL_STATE["start_time"]), 0)


def latency_p95(latencies_ms: list[float]) -> int:
    if not latencies_ms:
        return 120
    sorted_values = sorted(latencies_ms)
    index = max(int(0.95 * (len(sorted_values) - 1)), 0)
    return int(sorted_values[index])


def probe_endpoint(method: str, url: str) -> dict:
    started_at = time.perf_counter()
    try:
        response = requests.request(method, url, timeout=2)
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        payload = None
        try:
            payload = response.json()
        except ValueError:
            payload = response.text.strip()

        ok = 200 <= response.status_code < 400
        return {
            "ok": ok,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "payload": payload,
            "error": None,
        }
    except requests.RequestException as exc:
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        return {
            "ok": False,
            "status_code": None,
            "latency_ms": latency_ms,
            "payload": None,
            "error": str(exc),
        }


def probe_db_down_status() -> dict:
    container_name = DRILL_STATE["db_container_name"]
    db_running = container_is_running(container_name)
    health_result = probe_endpoint("GET", "http://127.0.0.1:5000/health")
    checkout_result = probe_endpoint("POST", "http://127.0.0.1:5000/checkout")

    print(f"[WARROOM backend] health check result={health_result}")
    print(f"[WARROOM backend] checkout probe result={checkout_result}")

    add_timeline_event("00:00 - Drill started")
    DRILL_STATE["probe_count"] += 2

    for result in (health_result, checkout_result):
        DRILL_STATE["latencies_ms"].append(result["latency_ms"])
        if result["ok"]:
            DRILL_STATE["success_count"] += 1
        else:
            DRILL_STATE["error_count"] += 1
            if DRILL_STATE["first_failure_time"] is None:
                DRILL_STATE["first_failure_time"] = max(elapsed_seconds(), 3)

    if not db_running:
        add_timeline_event("00:02 - warroom-db stopped")
        append_log("[db] container stopped")

    if not checkout_result["ok"]:
        add_timeline_event("00:03 - First 5xx response")
        append_log("[app] POST /checkout -> 500 database unavailable")

    if DRILL_STATE["error_count"] >= 2:
        add_timeline_event("00:05 - Error rate increasing")

    if DRILL_STATE["poll_count"] >= 5:
        add_timeline_event("00:10 - Drill complete")

    success_rate = int(
        round((DRILL_STATE["success_count"] / max(DRILL_STATE["probe_count"], 1)) * 100)
    )
    p95_latency = latency_p95(DRILL_STATE["latencies_ms"])

    append_log(
        f"[metrics] success_rate={success_rate} "
        f"error_count={DRILL_STATE['error_count']} p95_latency={p95_latency}"
    )

    app_status = "running" if health_result["ok"] else "degraded"
    db_status = "running" if db_running else "stopped"

    return {
        "app_status": app_status,
        "db_status": db_status,
        "success_rate": success_rate,
        "error_count": DRILL_STATE["error_count"],
        "p95_latency": p95_latency,
        "first_failure_time": DRILL_STATE["first_failure_time"],
        "timeline": list(DRILL_STATE["timeline"]),
    }


def build_real_db_down_evidence() -> dict:
    success_rate = max(0, min(100, int(round(
        (DRILL_STATE["success_count"] / max(DRILL_STATE["probe_count"], 1)) * 100
    ))))
    p95_latency = latency_p95(DRILL_STATE["latencies_ms"])

    return {
        "success_rate": success_rate,
        "p95_latency": p95_latency,
        "error_count": DRILL_STATE["error_count"],
        "first_failure_time": DRILL_STATE["first_failure_time"],
        "likely_cause": (
            "Checkout requests failed because the database became unavailable "
            "and no graceful fallback existed."
        ),
        "suggested_fix": (
            "Add retry handling, circuit breaker logic, and graceful fallback "
            "when the database is unreachable."
        ),
        "logs": list(DRILL_STATE["logs"]),
    }


def wait_for_demo_health(timeout_seconds: int = 15) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = probe_endpoint("GET", "http://127.0.0.1:5000/health")
        print(f"[WARROOM backend] health check result={result}")
        if result["ok"]:
            return
        time.sleep(1)

    raise HTTPException(
        status_code=500,
        detail="Demo app health endpoint did not recover after resetting the drill.",
    )


def build_battle_snapshot(drill_type: str, poll_count: int) -> dict:
    if drill_type == "db_down":
        snapshots = [
            {
                "app_status": "running",
                "db_status": "running",
                "success_rate": 100,
                "error_count": 0,
                "p95_latency": 120,
                "first_failure_time": None,
                "timeline": [
                    "00:00 - Drill started",
                ],
            },
            {
                "app_status": "running",
                "db_status": "running",
                "success_rate": 98,
                "error_count": 2,
                "p95_latency": 180,
                "first_failure_time": None,
                "timeline": [
                    "00:00 - Drill started",
                ],
            },
            {
                "app_status": "running",
                "db_status": "stopped",
                "success_rate": 91,
                "error_count": 8,
                "p95_latency": 420,
                "first_failure_time": 3,
                "timeline": [
                    "00:00 - Drill started",
                    "00:02 - warroom-db stopped",
                    "00:03 - First 5xx response",
                ],
            },
            {
                "app_status": "degraded",
                "db_status": "stopped",
                "success_rate": 81,
                "error_count": 21,
                "p95_latency": 860,
                "first_failure_time": 3,
                "timeline": [
                    "00:00 - Drill started",
                    "00:02 - warroom-db stopped",
                    "00:03 - First 5xx response",
                    "00:05 - Error rate increasing",
                ],
            },
            {
                "app_status": "degraded",
                "db_status": "stopped",
                "success_rate": 72,
                "error_count": 38,
                "p95_latency": 1240,
                "first_failure_time": 3,
                "timeline": [
                    "00:00 - Drill started",
                    "00:02 - warroom-db stopped",
                    "00:03 - First 5xx response",
                    "00:05 - Error rate increasing",
                    "00:10 - Drill complete",
                ],
            },
        ]

        index = min(max(poll_count - 1, 0), len(snapshots) - 1)
        return snapshots[index]

    if drill_type == "latency_spike":
        snapshots = [
            {
                "app_status": "running",
                "db_status": "running",
                "success_rate": 100,
                "error_count": 0,
                "p95_latency": 140,
                "first_failure_time": None,
                "timeline": ["00:00 - Drill started"],
            },
            {
                "app_status": "running",
                "db_status": "degraded",
                "success_rate": 96,
                "error_count": 2,
                "p95_latency": 480,
                "first_failure_time": None,
                "timeline": [
                    "00:00 - Drill started",
                    "00:02 - database latency rising",
                ],
            },
            {
                "app_status": "degraded",
                "db_status": "degraded",
                "success_rate": 88,
                "error_count": 7,
                "p95_latency": 920,
                "first_failure_time": 4,
                "timeline": [
                    "00:00 - Drill started",
                    "00:02 - database latency rising",
                    "00:04 - requests timing out",
                    "00:10 - Drill complete",
                ],
            },
        ]

        index = min(max((poll_count // 2), 0), len(snapshots) - 1)
        return snapshots[index]

    snapshots = [
        {
            "app_status": "running",
            "db_status": "running",
            "success_rate": 100,
            "error_count": 0,
            "p95_latency": 120,
            "first_failure_time": None,
            "timeline": ["00:00 - Drill started"],
        },
        {
            "app_status": "degraded",
            "db_status": "running",
            "success_rate": 89,
            "error_count": 14,
            "p95_latency": 510,
            "first_failure_time": 3,
            "timeline": [
                "00:00 - Drill started",
                "00:03 - request volume increasing",
                "00:05 - Error rate increasing",
                "00:10 - Drill complete",
            ],
        },
    ]

    index = min(max((poll_count // 3), 0), len(snapshots) - 1)
    return snapshots[index]


def build_evidence(drill_type: str) -> dict:
    if drill_type == "db_down":
        return {
            "success_rate": 72,
            "p95_latency": 1240,
            "error_count": 38,
            "first_failure_time": 3,
            "likely_cause": (
                "Checkout requests failed because the database became unavailable "
                "and no graceful fallback existed."
            ),
            "suggested_fix": (
                "Add retry handling, circuit breaker logic, and graceful fallback "
                "when the database is unreachable."
            ),
            "logs": [
                "[app] POST /checkout -> 500 database unavailable",
                "[db] container stopped",
                "[metrics] success_rate=72 error_count=38 p95_latency=1240",
            ],
        }

    if drill_type == "latency_spike":
        return {
            "success_rate": 88,
            "p95_latency": 920,
            "error_count": 7,
            "first_failure_time": 4,
            "likely_cause": "Database latency increased sharply and requests timed out.",
            "suggested_fix": "Add timeouts, retries with limits, and isolate slow dependencies.",
            "logs": [
                "[proxy] injected downstream latency",
                "[app] GET /checkout -> 504 upstream timeout",
                "[metrics] success_rate=88 error_count=7 p95_latency=920",
            ],
        }

    return {
        "success_rate": 84,
        "p95_latency": 510,
        "error_count": 14,
        "first_failure_time": 3,
        "likely_cause": "Request volume exceeded app capacity and error rates climbed.",
        "suggested_fix": "Add rate limiting, autoscaling, and queue protection under load.",
        "logs": [
            "[load] request flood started",
            "[app] POST /checkout -> 503 overloaded",
            "[metrics] success_rate=84 error_count=14 p95_latency=510",
        ],
    }


def classify_fear_text(fear: str) -> str:
    lower_fear = fear.lower()
    mentions_database = "database" in lower_fear or "db" in lower_fear
    mentions_down = "down" in lower_fear or "fail" in lower_fear
    mentions_latency = "slow" in lower_fear or "latency" in lower_fear
    mentions_flood = (
        "flood" in lower_fear
        or "traffic" in lower_fear
        or "requests" in lower_fear
    )

    if mentions_database and mentions_down:
        return "db_down"
    if mentions_latency:
        return "latency_spike"
    if mentions_flood:
        return "request_flood"
    return "db_down"


@app.get("/")
def root():
    print("[WARROOM backend] GET /")
    return {"message": "WARROOM backend running"}


@app.post("/classify")
def classify(request: ClassifyRequest):
    print(f"[WARROOM backend] POST /classify fear={request.fear!r}")
    drill_type = classify_fear_text(request.fear)
    return DRILL_CONFIG[drill_type]


@app.post("/drill/start")
def start_drill(request: StartDrillRequest):
    drill_id = "demo-drill-1"
    reset_drill_state()
    DRILL_STATE["drill_id"] = drill_id
    DRILL_STATE["drill_type"] = request.drill_type
    DRILL_STATE["status"] = "running"
    DRILL_STATE["poll_count"] = 0
    DRILL_STATE["start_time"] = time.time()

    if request.drill_type == "db_down":
        container_name = resolve_db_container_name()
        DRILL_STATE["db_container_name"] = container_name
        DRILL_STATE["timeline"] = ["00:00 - Drill started"]
        stop_container(container_name)
        print(
            f"[WARROOM backend] drill start "
            f"drill_id={drill_id} drill_type={request.drill_type} "
            f"container={container_name}"
        )
    else:
        DRILL_STATE["evidence"] = build_evidence(request.drill_type)
        print(
            f"[WARROOM backend] drill start "
            f"drill_id={drill_id} drill_type={request.drill_type}"
        )

    return {
        "drill_id": drill_id,
        "status": "started",
    }


@app.get("/drill/status")
def drill_status():
    if not DRILL_STATE["drill_id"]:
        print("[WARROOM backend] GET /drill/status no active drill")
        return {
            "drill_id": None,
            "status": "idle",
            "app_status": "running",
            "db_status": "running",
            "success_rate": 100,
            "error_count": 0,
            "p95_latency": 120,
            "first_failure_time": None,
            "timeline": [],
        }

    if DRILL_STATE["status"] == "running":
        DRILL_STATE["poll_count"] += 1
        print(
            f"[WARROOM backend] GET /drill/status "
            f"drill_id={DRILL_STATE['drill_id']} poll_count={DRILL_STATE['poll_count']}"
        )

        if DRILL_STATE["poll_count"] >= 5:
            DRILL_STATE["status"] = "complete"
            print(
                f"[WARROOM backend] drill completion "
                f"drill_id={DRILL_STATE['drill_id']}"
            )
    else:
        print(
            f"[WARROOM backend] GET /drill/status "
            f"drill_id={DRILL_STATE['drill_id']} poll_count={DRILL_STATE['poll_count']}"
        )

    if DRILL_STATE["drill_type"] == "db_down":
        snapshot = probe_db_down_status()
        if DRILL_STATE["status"] == "complete":
            DRILL_STATE["evidence"] = build_real_db_down_evidence()
    else:
        snapshot = build_battle_snapshot(
            DRILL_STATE["drill_type"],
            DRILL_STATE["poll_count"],
        )

    return {
        "drill_id": DRILL_STATE["drill_id"],
        "status": DRILL_STATE["status"],
        **snapshot,
    }


@app.get("/drill/evidence")
def drill_evidence():
    print(
        f"[WARROOM backend] GET /drill/evidence "
        f"drill_id={DRILL_STATE['drill_id']} status={DRILL_STATE['status']}"
    )

    if DRILL_STATE["drill_type"] == "db_down" and DRILL_STATE["status"] != "idle":
        print("[WARROOM backend] evidence fetch for real db_down drill")
        return DRILL_STATE["evidence"] or build_real_db_down_evidence()

    if not DRILL_STATE["evidence"]:
        return {
            "success_rate": 100,
            "p95_latency": 120,
            "error_count": 0,
            "first_failure_time": None,
            "likely_cause": "No completed drill evidence is available yet.",
            "suggested_fix": "Start a drill and allow it to complete.",
            "logs": [],
        }

    return DRILL_STATE["evidence"]


@app.post("/drill/reset")
def reset_drill():
    print(f"[WARROOM backend] POST /drill/reset drill_id={DRILL_STATE['drill_id']}")

    if DRILL_STATE["drill_type"] == "db_down":
        container_name = DRILL_STATE["db_container_name"] or resolve_db_container_name()
        start_container(container_name)
        wait_for_demo_health()
        print(
            f"[WARROOM backend] reset success "
            f"drill_id={DRILL_STATE['drill_id']} container={container_name}"
        )

    reset_drill_state()
    return {"status": "reset"}
