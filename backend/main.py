from typing import Literal

from fastapi import FastAPI
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
    "evidence": None,
}


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
    DRILL_STATE["drill_id"] = drill_id
    DRILL_STATE["drill_type"] = request.drill_type
    DRILL_STATE["status"] = "running"
    DRILL_STATE["poll_count"] = 0
    DRILL_STATE["evidence"] = build_evidence(request.drill_type)

    print(
        f"[WARROOM backend] POST /drill/start "
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
    DRILL_STATE["drill_id"] = None
    DRILL_STATE["drill_type"] = None
    DRILL_STATE["status"] = "idle"
    DRILL_STATE["poll_count"] = 0
    DRILL_STATE["evidence"] = None
    return {"status": "reset"}
