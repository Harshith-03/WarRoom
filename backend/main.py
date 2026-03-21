import json
import subprocess
import time
from typing import Literal

import requests

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


app = FastAPI(title="WARROOM Backend")
APP_BASE_URL = "http://127.0.0.1:5001"
MCP_BASE_URL = "http://127.0.0.1:9100"

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
    duration: str | None = None
    intensity: str | None = None


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
    "proxy_name": None,
    "timeline": [],
    "logs": [],
    "latencies_ms": [],
    "success_count": 0,
    "probe_count": 0,
    "error_count": 0,
    "db_stop_time": None,
    "first_failure_time": None,
    "latency_injection_time": None,
    "latency_delay_time": None,
    "mcp_activity": [],
    "evidence": None,
}


def reset_drill_state() -> None:
    DRILL_STATE["drill_id"] = None
    DRILL_STATE["drill_type"] = None
    DRILL_STATE["status"] = "idle"
    DRILL_STATE["poll_count"] = 0
    DRILL_STATE["start_time"] = None
    DRILL_STATE["db_container_name"] = None
    DRILL_STATE["proxy_name"] = None
    DRILL_STATE["timeline"] = []
    DRILL_STATE["logs"] = []
    DRILL_STATE["latencies_ms"] = []
    DRILL_STATE["success_count"] = 0
    DRILL_STATE["probe_count"] = 0
    DRILL_STATE["error_count"] = 0
    DRILL_STATE["db_stop_time"] = None
    DRILL_STATE["first_failure_time"] = None
    DRILL_STATE["latency_injection_time"] = None
    DRILL_STATE["latency_delay_time"] = None
    DRILL_STATE["mcp_activity"] = []
    DRILL_STATE["evidence"] = None


def run_podman_command(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["podman", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def call_mcp_tool(tool_name: str, payload: dict) -> dict:
    print(f"[WARROOM backend] calling MCP tool {tool_name}")
    try:
        response = requests.post(
            f"{MCP_BASE_URL}/tools/{tool_name}",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"MCP tool {tool_name} failed: {exc}",
        ) from exc

    data = response.json()
    if not data.get("ok", False):
        raise HTTPException(
            status_code=502,
            detail=f"MCP tool {tool_name} returned an unsuccessful response.",
        )

    print(f"[WARROOM backend] MCP {tool_name} success")
    return data


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
    probe_name = "health" if url.endswith("/health") else "checkout"
    print(f"[WARROOM backend] probing {probe_name} url={url}")
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
    health_result = probe_endpoint("GET", f"{APP_BASE_URL}/health")
    checkout_result = probe_endpoint("POST", f"{APP_BASE_URL}/checkout")

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

    if not db_running:
        if DRILL_STATE["db_stop_time"] is None:
            DRILL_STATE["db_stop_time"] = max(elapsed_seconds(), 0)
        add_timeline_event(
            f"00:{str(DRILL_STATE['db_stop_time']).zfill(2)} - warroom-db stopped"
        )
        append_log("[db] container stopped")

    if not checkout_result["ok"]:
        if DRILL_STATE["first_failure_time"] is None:
            failure_second = max(elapsed_seconds(), 1)
            if DRILL_STATE["db_stop_time"] is not None:
                failure_second = max(failure_second, DRILL_STATE["db_stop_time"])
            DRILL_STATE["first_failure_time"] = failure_second
        add_timeline_event(
            f"00:{str(DRILL_STATE['first_failure_time']).zfill(2)} - First 5xx response"
        )
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


def probe_latency_spike_status() -> dict:
    health_result = probe_endpoint("GET", f"{APP_BASE_URL}/health")
    checkout_result = probe_endpoint("POST", f"{APP_BASE_URL}/checkout")

    print(f"[WARROOM backend] health check result={health_result}")
    print(f"[WARROOM backend] checkout probe result={checkout_result}")

    add_timeline_event("00:00 - Drill started")
    if DRILL_STATE["latency_injection_time"] is None:
        DRILL_STATE["latency_injection_time"] = max(elapsed_seconds(), 0)
    add_timeline_event(
        f"00:{str(DRILL_STATE['latency_injection_time']).zfill(2)} - Latency injection applied"
    )

    DRILL_STATE["probe_count"] += 2
    for result in (health_result, checkout_result):
        DRILL_STATE["latencies_ms"].append(result["latency_ms"])
        if result["ok"]:
            DRILL_STATE["success_count"] += 1
        else:
            DRILL_STATE["error_count"] += 1

    if checkout_result["latency_ms"] >= 800:
        if DRILL_STATE["latency_delay_time"] is None:
            DRILL_STATE["latency_delay_time"] = max(
                elapsed_seconds(),
                DRILL_STATE["latency_injection_time"] or 0,
            )
        add_timeline_event(
            f"00:{str(DRILL_STATE['latency_delay_time']).zfill(2)} - Checkout response delay increased"
        )
        append_log(f"[proxy] injected database latency via {DRILL_STATE['proxy_name'] or 'warroom-db-proxy'}")

    if not checkout_result["ok"] and DRILL_STATE["first_failure_time"] is None:
        DRILL_STATE["first_failure_time"] = max(
            elapsed_seconds(),
            DRILL_STATE["latency_delay_time"] or DRILL_STATE["latency_injection_time"] or 1,
            1,
        )
        add_timeline_event(
            f"00:{str(DRILL_STATE['first_failure_time']).zfill(2)} - First 5xx response"
        )

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

    app_status = "degraded" if (p95_latency >= 800 or not health_result["ok"] or not checkout_result["ok"]) else "running"
    db_status = "running"

    return {
        "app_status": app_status,
        "db_status": db_status,
        "success_rate": success_rate,
        "error_count": DRILL_STATE["error_count"],
        "p95_latency": p95_latency,
        "first_failure_time": DRILL_STATE["first_failure_time"],
        "timeline": list(DRILL_STATE["timeline"]),
    }


def classify_fear_with_ollama(fear: str) -> str:
    print("[WARROOM backend] starting Ollama classification")

    prompt = (
        "Classify the following fear into exactly one supported drill type.\n"
        "Supported drill types:\n"
        "- db_down\n"
        "- latency_spike\n"
        "- request_flood\n"
        "Return valid JSON only with this shape: {\"drill_type\": \"...\"}\n"
        "Do not explain reasoning.\n"
        "If uncertain, choose the closest supported category.\n"
        "Mapping guidance:\n"
        "- \"database goes down\" -> db_down\n"
        "- \"database gets slow\" -> latency_spike\n"
        "- \"traffic spike\" -> request_flood\n"
        "- \"requests flood checkout\" -> request_flood\n\n"
        f"Fear: {fear}"
    )

    response = requests.post(
        "http://127.0.0.1:11434/api/generate",
        json={
            "model": "llama3",
            "stream": False,
            "prompt": prompt,
        },
        timeout=20,
    )
    response.raise_for_status()

    response_data = response.json()
    raw_text = response_data.get("response", "").strip()

    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    classification = json.loads(raw_text)
    drill_type = classification["drill_type"]

    if drill_type not in DRILL_CONFIG:
        raise ValueError(f"Unsupported drill type from Ollama: {drill_type}")

    print(f"[WARROOM backend] Ollama classification success drill_type={drill_type}")
    return drill_type


def generate_expected_impact_with_ollama(
    fear: str,
    drill_type: str,
    label: str,
    target_service: str,
    duration: str,
) -> str:
    print("[WARROOM backend] starting Ollama expected_impact generation")

    prompt = (
        "You are generating expected impact text for a controlled resilience drill.\n"
        "Supported drill types are fixed and already chosen.\n"
        "Do not invent new drill types.\n"
        "Do not invent new target services.\n"
        "Describe the likely user-visible impact for this drill type in one concise sentence.\n"
        "Keep it grounded and practical.\n"
        "Do not output commands.\n"
        "Return valid JSON only with this shape: {\"expected_impact\": \"...\"}\n"
        "Example styles:\n"
        "- db_down: \"Checkout requests will likely fail during the outage window because the application depends directly on the database.\"\n"
        "- latency_spike: \"Checkout responses may slow down or time out as database latency increases.\"\n"
        "- request_flood: \"Checkout success rate may drop and response times may rise under sustained concurrent traffic.\"\n\n"
        f"Fear: {fear}\n"
        f"Drill type: {drill_type}\n"
        f"Label: {label}\n"
        f"Target service: {target_service}\n"
        f"Default duration: {duration}\n"
    )

    response = requests.post(
        "http://127.0.0.1:11434/api/generate",
        json={
            "model": "llama3",
            "stream": False,
            "prompt": prompt,
        },
        timeout=20,
    )
    response.raise_for_status()

    response_data = response.json()
    raw_text = response_data.get("response", "").strip()

    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    impact = json.loads(raw_text)["expected_impact"]
    print("[WARROOM backend] Ollama expected_impact success")
    return impact


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
        "summary": (
            "The drill indicates checkout failures after the database became "
            "unavailable."
        ),
        "logs": list(DRILL_STATE["logs"]),
        "timeline": list(DRILL_STATE["timeline"]),
    }


def build_real_latency_evidence() -> dict:
    success_rate = max(
        0,
        min(100, int(round((DRILL_STATE["success_count"] / max(DRILL_STATE["probe_count"], 1)) * 100))),
    )
    p95_latency = latency_p95(DRILL_STATE["latencies_ms"])

    return {
        "success_rate": success_rate,
        "p95_latency": p95_latency,
        "error_count": DRILL_STATE["error_count"],
        "first_failure_time": DRILL_STATE["first_failure_time"],
        "likely_cause": (
            "Checkout slowed because database requests were artificially delayed, "
            "and the application had limited protection against dependency latency."
        ),
        "suggested_fix": (
            "Add tighter timeouts, bounded retries, and circuit-breaker or fallback "
            "behavior so slow database calls do not degrade checkout."
        ),
        "summary": (
            "The drill indicates database latency increased checkout response times "
            "and degraded the user experience."
        ),
        "logs": list(DRILL_STATE["logs"]),
        "timeline": list(DRILL_STATE["timeline"]),
    }


def generate_ollama_verdict(evidence_input: dict) -> dict:
    print("[WARROOM backend] starting Ollama verdict generation")

    prompt = (
        "You are writing a resilience verdict for an engineering drill.\n"
        "Use only the provided evidence: metrics, logs, and timeline.\n"
        "Do not invent facts or systems that are not in the evidence.\n"
        "Focus on application and system failure reasoning, not just the raw infrastructure event.\n"
        "Explain what dependency or resilience weakness caused user-facing failure.\n"
        "Avoid shallow advice.\n"
        "Bad likely_cause example: \"warroom-db stopped\"\n"
        "Better likely_cause example: "
        "\"The checkout path had a hard dependency on the database and no graceful "
        "fallback, so requests failed immediately after the database became unavailable.\"\n"
        "Bad suggested_fix example: \"Ensure warroom-db is running\"\n"
        "Better suggested_fix example: "
        "\"Add retry logic, circuit breaker behavior, and a fallback response when "
        "the database is unavailable.\"\n"
        "Suggested fixes should emphasize resilience patterns such as retry handling, "
        "circuit breaker behavior, graceful degradation, fallback responses, and "
        "dependency protection where supported by the evidence.\n"
        "If the evidence is weak, say uncertainty clearly.\n"
        "Keep the response concise:\n"
        "- likely_cause: 1-2 sentences\n"
        "- suggested_fix: 1-2 sentences\n"
        "- summary: 1 sentence\n"
        "Return valid JSON only with keys: likely_cause, suggested_fix, summary.\n\n"
        f"Evidence:\n{json.dumps(evidence_input, indent=2)}"
    )

    response = requests.post(
        "http://127.0.0.1:11434/api/generate",
        json={
            "model": "llama3",
            "stream": False,
            "prompt": prompt,
        },
        timeout=20,
    )
    response.raise_for_status()

    response_data = response.json()
    raw_text = response_data.get("response", "").strip()

    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    verdict = json.loads(raw_text)
    print("[WARROOM backend] Ollama success")
    return {
        "likely_cause": verdict["likely_cause"],
        "suggested_fix": verdict["suggested_fix"],
        "summary": verdict["summary"],
    }


def build_fallback_action_plan(evidence: dict) -> dict:
    reasoning_text = " ".join(
        [
            evidence.get("likely_cause", ""),
            evidence.get("suggested_fix", ""),
            evidence.get("summary", ""),
        ]
    ).lower()

    if DRILL_STATE["drill_type"] == "db_down" or "database" in reasoning_text:
        return {
            "do_now": [
                "Restore database availability and confirm checkout requests recover.",
                "Pause any risky traffic or drill actions until the app is stable again.",
                "Verify that new checkout failures stop after the database comes back.",
            ],
            "fix_in_code": [
                "Add graceful fallback behavior when the database is unreachable.",
                "Add retry protection with limits around checkout database calls.",
                "Introduce circuit-breaker behavior so dependency failure does not cascade immediately.",
            ],
            "improve_later": [
                "Add automated dependency failure tests for the checkout path.",
                "Improve health checks and alerting around database reachability.",
                "Document a recovery playbook for database outage scenarios.",
            ],
        }

    return {
        "do_now": [
            "Stabilize the affected service and confirm customer-facing failures stop.",
            "Review the captured evidence and identify the weakest dependency in the flow.",
            "Communicate current impact and recovery status to the team.",
        ],
        "fix_in_code": [
            "Add defensive handling around the failing path so errors degrade more safely.",
            "Add retries, timeouts, or circuit-breaker protection where the evidence shows weakness.",
            "Cover the failure path with an automated resilience test.",
        ],
        "improve_later": [
            "Add clearer runbooks and alerts for this class of failure.",
            "Track user-facing error rate and recovery time after dependency incidents.",
            "Run the same drill regularly to verify the fix stays effective.",
        ],
    }


def generate_ollama_action_plan(action_plan_input: dict) -> dict:
    print("[WARROOM backend] starting Ollama action plan generation")

    prompt = (
        "You are generating an action plan after a resilience drill.\n"
        "Use only the provided evidence.\n"
        "Do not invent facts, systems, or metrics.\n"
        "Keep actions practical, concise, and useful for engineers.\n"
        "Return valid JSON only in this shape:\n"
        "{\n"
        '  "do_now": ["...", "...", "..."],\n'
        '  "fix_in_code": ["...", "...", "..."],\n'
        '  "improve_later": ["...", "...", "..."]\n'
        "}\n"
        "Each item should be one short action.\n\n"
        f"Evidence:\n{json.dumps(action_plan_input, indent=2)}"
    )

    response = requests.post(
        "http://127.0.0.1:11434/api/generate",
        json={
            "model": "llama3",
            "stream": False,
            "prompt": prompt,
        },
        timeout=20,
    )
    response.raise_for_status()

    response_data = response.json()
    raw_text = response_data.get("response", "").strip()

    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    action_plan = json.loads(raw_text)
    print("[WARROOM backend] Ollama action plan success")
    return {
        "do_now": action_plan["do_now"][:3],
        "fix_in_code": action_plan["fix_in_code"][:3],
        "improve_later": action_plan["improve_later"][:3],
    }


def wait_for_demo_health(timeout_seconds: int = 15) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = probe_endpoint("GET", f"{APP_BASE_URL}/health")
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
            "summary": (
                "The drill indicates checkout failures after the database became "
                "unavailable."
            ),
            "logs": [
                "[app] POST /checkout -> 500 database unavailable",
                "[db] container stopped",
                "[metrics] success_rate=72 error_count=38 p95_latency=1240",
            ],
            "timeline": [
                "00:00 - Drill started",
                "00:02 - warroom-db stopped",
                "00:03 - First 5xx response",
                "00:05 - Error rate increasing",
                "00:10 - Drill complete",
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
            "summary": "The drill indicates dependency latency caused timeouts and failures.",
            "logs": [
                "[proxy] injected downstream latency",
                "[app] GET /checkout -> 504 upstream timeout",
                "[metrics] success_rate=88 error_count=7 p95_latency=920",
            ],
            "timeline": [
                "00:00 - Drill started",
                "00:02 - Latency injection applied",
                "00:02 - Checkout response delay increased",
                "00:10 - Drill complete",
            ],
        }

    return {
        "success_rate": 84,
        "p95_latency": 510,
        "error_count": 14,
        "first_failure_time": 3,
        "likely_cause": "Request volume exceeded app capacity and error rates climbed.",
        "suggested_fix": "Add rate limiting, autoscaling, and queue protection under load.",
        "summary": "The drill indicates the app degraded under sustained request load.",
        "logs": [
            "[load] request flood started",
            "[app] POST /checkout -> 503 overloaded",
            "[metrics] success_rate=84 error_count=14 p95_latency=510",
        ],
        "timeline": [
            "00:00 - Drill started",
            "00:03 - request volume increasing",
            "00:05 - Error rate increasing",
            "00:10 - Drill complete",
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
    try:
        drill_type = classify_fear_with_ollama(request.fear)
    except Exception as exc:
        print(f"[WARROOM backend] Ollama classification fallback: {exc}")
        drill_type = classify_fear_text(request.fear)

    response = dict(DRILL_CONFIG[drill_type])

    try:
        response["expected_impact"] = generate_expected_impact_with_ollama(
            fear=request.fear,
            drill_type=response["drill_type"],
            label=response["label"],
            target_service=response["target_service"],
            duration=response["duration"],
        )
    except Exception as exc:
        print(f"[WARROOM backend] Ollama expected_impact fallback: {exc}")

    return response


@app.post("/drill/start")
def start_drill(request: StartDrillRequest):
    drill_id = "demo-drill-1"
    reset_drill_state()
    DRILL_STATE["drill_id"] = drill_id
    DRILL_STATE["drill_type"] = request.drill_type
    DRILL_STATE["status"] = "running"
    DRILL_STATE["poll_count"] = 0
    DRILL_STATE["start_time"] = time.time()

    if request.drill_type in {"db_down", "latency_spike"}:
        result = call_mcp_tool(
            "run_drill",
            {
                "drill_type": request.drill_type,
                "duration": request.duration,
                "intensity": request.intensity,
            },
        )
        DRILL_STATE["db_container_name"] = result.get("container")
        DRILL_STATE["proxy_name"] = result.get("proxy")
        DRILL_STATE["mcp_activity"] = result.get("mcp_activity", [])
        DRILL_STATE["timeline"] = ["00:00 - Drill started"]
        print(
            f"[WARROOM backend] drill start "
            f"drill_id={drill_id} drill_type={request.drill_type} "
            f"container={DRILL_STATE['db_container_name']} proxy={DRILL_STATE['proxy_name']}"
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
            "mcp_activity": [],
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
    elif DRILL_STATE["drill_type"] == "latency_spike":
        snapshot = probe_latency_spike_status()
        if DRILL_STATE["status"] == "complete":
            DRILL_STATE["evidence"] = build_real_latency_evidence()
    else:
        snapshot = build_battle_snapshot(
            DRILL_STATE["drill_type"],
            DRILL_STATE["poll_count"],
        )

    return {
        "drill_id": DRILL_STATE["drill_id"],
        "status": DRILL_STATE["status"],
        "mcp_activity": DRILL_STATE["mcp_activity"],
        **snapshot,
    }


@app.get("/drill/evidence")
def drill_evidence():
    print(
        f"[WARROOM backend] GET /drill/evidence "
        f"drill_id={DRILL_STATE['drill_id']} status={DRILL_STATE['status']}"
    )

    if DRILL_STATE["drill_type"] in {"db_down", "latency_spike"} and DRILL_STATE["status"] != "idle":
        print(f"[WARROOM backend] evidence fetch for real {DRILL_STATE['drill_type']} drill")
        if DRILL_STATE["drill_type"] == "db_down":
            evidence = DRILL_STATE["evidence"] or build_real_db_down_evidence()
        else:
            evidence = DRILL_STATE["evidence"] or build_real_latency_evidence()
        ollama_input = {
            "drill_type": DRILL_STATE["drill_type"],
            "success_rate": evidence["success_rate"],
            "p95_latency": evidence["p95_latency"],
            "error_count": evidence["error_count"],
            "first_failure_time": evidence["first_failure_time"],
            "timeline": list(DRILL_STATE["timeline"]),
            "logs": evidence["logs"],
        }
        try:
            evidence.update(generate_ollama_verdict(ollama_input))
        except Exception as exc:
            print(f"[WARROOM backend] Ollama fallback: {exc}")
        return evidence

    if not DRILL_STATE["evidence"]:
        return {
            "success_rate": 100,
            "p95_latency": 120,
            "error_count": 0,
            "first_failure_time": None,
            "likely_cause": "No completed drill evidence is available yet.",
            "suggested_fix": "Start a drill and allow it to complete.",
            "summary": "No completed drill evidence is available yet.",
            "logs": [],
        }

    return DRILL_STATE["evidence"]


@app.get("/drill/action-plan")
def drill_action_plan():
    print(
        f"[WARROOM backend] GET /drill/action-plan "
        f"drill_id={DRILL_STATE['drill_id']} status={DRILL_STATE['status']}"
    )

    if DRILL_STATE["drill_type"] in {"db_down", "latency_spike"} and DRILL_STATE["status"] != "idle":
        if DRILL_STATE["drill_type"] == "db_down":
            evidence = DRILL_STATE["evidence"] or build_real_db_down_evidence()
        else:
            evidence = DRILL_STATE["evidence"] or build_real_latency_evidence()
    else:
        evidence = DRILL_STATE["evidence"] or build_evidence(
            DRILL_STATE["drill_type"] or "db_down"
        )

    action_plan_input = {
        "drill_type": DRILL_STATE["drill_type"] or "db_down",
        "likely_cause": evidence.get("likely_cause"),
        "suggested_fix": evidence.get("suggested_fix"),
        "summary": evidence.get("summary"),
        "success_rate": evidence.get("success_rate"),
        "error_count": evidence.get("error_count"),
        "p95_latency": evidence.get("p95_latency"),
        "first_failure_time": evidence.get("first_failure_time"),
        "logs": evidence.get("logs", []),
        "timeline": list(DRILL_STATE["timeline"]),
    }

    try:
        return generate_ollama_action_plan(action_plan_input)
    except Exception as exc:
        print(f"[WARROOM backend] Ollama action plan fallback: {exc}")
        return build_fallback_action_plan(evidence)


@app.post("/drill/reset")
def reset_drill():
    print(f"[WARROOM backend] POST /drill/reset drill_id={DRILL_STATE['drill_id']}")

    if DRILL_STATE["drill_type"] in {"db_down", "latency_spike"}:
        result = call_mcp_tool(
            "reset",
            {
                "drill_id": DRILL_STATE["drill_id"],
            },
        )
        DRILL_STATE["mcp_activity"] = result.get("mcp_activity", DRILL_STATE["mcp_activity"])
        print(
            f"[WARROOM backend] reset success "
            f"drill_id={DRILL_STATE['drill_id']} "
            f"container={result.get('container')} proxy={result.get('proxy')}"
        )

    reset_drill_state()
    return {"status": "reset"}
