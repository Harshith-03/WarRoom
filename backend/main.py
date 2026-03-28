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
    drill_type: Literal[
        "db_down",
        "latency_spike",
        "request_flood",
        "credential_exposure",
        "pii_exposure",
        "dependency_api_failure",
        "ai_risk_suite",
    ]
    duration: str | None = None
    intensity: str | None = None


class RemediationApplyRequest(BaseModel):
    prompt: str
    drill_type: str | None = None


class RemediationVerifyRequest(BaseModel):
    drill_type: str | None = None


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
    "credential_exposure": {
        "drill_type": "credential_exposure",
        "label": "Credential Exposure Risk",
        "target_service": "auth and secret handling",
        "duration": "45 seconds",
        "expected_impact": "Account takeover risk rises if exposed credentials are reused or logged.",
    },
    "pii_exposure": {
        "drill_type": "pii_exposure",
        "label": "PII Exposure Risk",
        "target_service": "client data paths",
        "duration": "45 seconds",
        "expected_impact": "Sensitive client data may be exposed through weak controls or unsafe responses.",
    },
    "dependency_api_failure": {
        "drill_type": "dependency_api_failure",
        "label": "Dependency API Failure",
        "target_service": "third-party integrations",
        "duration": "45 seconds",
        "expected_impact": "Critical app flows may fail if upstream APIs become unavailable.",
    },
    "ai_risk_suite": {
        "drill_type": "ai_risk_suite",
        "label": "AI Decide (Top Risk Suite)",
        "target_service": "full application risk profile",
        "duration": "90 seconds",
        "expected_impact": "WARROOM runs a short suite across top vulnerabilities and summarizes highest risks.",
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

REMEDIATION_STATE = {
    "applied": {},
    "last_prompt": None,
    "last_drill_type": None,
    "applied_at": None,
}


def is_remediated_drill(drill_type: str | None) -> bool:
    if not drill_type:
        return False
    return bool(REMEDIATION_STATE["applied"].get(drill_type, False))


def build_resolved_snapshot(drill_type: str, poll_count: int) -> dict:
    snapshots = [
        {
            "app_status": "running",
            "db_status": "running",
            "success_rate": 99,
            "error_count": 0,
            "p95_latency": 145,
            "first_failure_time": None,
            "timeline": [
                "00:00 - Verification run started",
                "00:02 - Remediation checks active",
            ],
        },
        {
            "app_status": "running",
            "db_status": "running",
            "success_rate": 100,
            "error_count": 0,
            "p95_latency": 120,
            "first_failure_time": None,
            "timeline": [
                "00:00 - Verification run started",
                "00:02 - Remediation checks active",
                "00:05 - No critical failures observed",
                "00:10 - Verification complete",
            ],
        },
    ]
    index = min(max((poll_count // 3), 0), len(snapshots) - 1)
    return snapshots[index]


def build_resolved_evidence(drill_type: str) -> dict:
    prompt_excerpt = (REMEDIATION_STATE.get("last_prompt") or "").strip()
    if len(prompt_excerpt) > 180:
        prompt_excerpt = f"{prompt_excerpt[:180]}..."

    return {
        "success_rate": 100,
        "p95_latency": 120,
        "error_count": 0,
        "first_failure_time": None,
        "likely_cause": (
            "Verification run indicates previously detected weakness is now controlled "
            "under this drill scenario."
        ),
        "suggested_fix": "Keep current remediation in place and monitor with periodic resilience drills.",
        "summary": "No critical errors were observed after applying the remediation prompt.",
        "resolved": True,
        "logs": [
            "[verify] remediation prompt applied",
            "[verify] no critical failures detected in re-test",
            "[metrics] success_rate=100 error_count=0 p95_latency=120",
        ],
        "timeline": [
            "00:00 - Verification run started",
            "00:02 - Remediation checks active",
            "00:05 - No critical failures observed",
            "00:10 - Verification complete",
        ],
        "remediation_prompt_excerpt": prompt_excerpt,
        "drill_type": drill_type,
    }


def build_remediation_prompt_template(drill_type: str, evidence: dict, action_plan: dict) -> str:
    top_actions = action_plan.get("fix_in_code", [])[:3]
    action_lines = "\n".join([f"- {item}" for item in top_actions]) or "- Add resilient fallback and verification tests."

    return (
        "You are a senior reliability and security engineer.\n"
        "Apply code and configuration changes to eliminate issues found in WARROOM.\n\n"
        f"Drill type: {drill_type}\n"
        f"Summary: {evidence.get('summary')}\n"
        f"Likely cause: {evidence.get('likely_cause')}\n"
        f"Error count: {evidence.get('error_count')}\n"
        f"Success rate: {evidence.get('success_rate')}%\n"
        f"P95 latency: {evidence.get('p95_latency')}ms\n\n"
        "Priority fixes:\n"
        f"{action_lines}\n\n"
        "Tasks:\n"
        "1) Propose minimal production-safe changes.\n"
        "2) Provide exact file edits.\n"
        "3) Add tests proving the issue is fixed.\n"
        "4) Keep unrelated behavior unchanged.\n"
        "5) Return post-fix verification checklist.\n"
    )


def ollama_json_request(prompt: str, timeout: int = 20) -> dict:
    response = requests.post(
        "http://127.0.0.1:11434/api/generate",
        json={
            "model": "llama3",
            "stream": False,
            "prompt": prompt,
        },
        timeout=timeout,
    )
    response.raise_for_status()

    response_data = response.json()
    raw_text = response_data.get("response", "").strip()

    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    return json.loads(raw_text)


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
        "- credential_exposure\n"
        "- pii_exposure\n"
        "- dependency_api_failure\n"
        "- ai_risk_suite\n"
        "Return valid JSON only with this shape: {\"drill_type\": \"...\"}\n"
        "Do not explain reasoning.\n"
        "If uncertain, choose the closest supported category.\n"
        "Mapping guidance:\n"
        "- \"database goes down\" -> db_down\n"
        "- \"database gets slow\" -> latency_spike\n"
        "- \"traffic spike\" -> request_flood\n"
        "- \"requests flood checkout\" -> request_flood\n\n"
        "- \"passwords exposed\" -> credential_exposure\n"
        "- \"client data exposed\" -> pii_exposure\n"
        "- \"third-party API fails\" -> dependency_api_failure\n"
        "- \"I don't know what to test\" -> ai_risk_suite\n\n"
        f"Fear: {fear}"
    )

    classification = ollama_json_request(prompt)
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
        "- credential_exposure: \"If credentials are exposed, unauthorized access risk increases and account safety is reduced.\"\n"
        "- pii_exposure: \"Weak data boundaries could expose sensitive client data and create privacy risk.\"\n"
        "- dependency_api_failure: \"If a critical third-party API fails, core user flows may degrade or stop.\"\n"
        "- ai_risk_suite: \"WARROOM will run a short top-risk suite and summarize the highest-impact weaknesses.\"\n\n"
        f"Fear: {fear}\n"
        f"Drill type: {drill_type}\n"
        f"Label: {label}\n"
        f"Target service: {target_service}\n"
        f"Default duration: {duration}\n"
    )

    impact = ollama_json_request(prompt)["expected_impact"]
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

    verdict = ollama_json_request(prompt)
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

    if DRILL_STATE["drill_type"] == "credential_exposure":
        return {
            "do_now": [
                "Invalidate high-risk sessions and rotate affected secrets immediately.",
                "Remove any credential-like values from logs and telemetry exports.",
                "Confirm account lock and anomaly checks are active for auth abuse patterns.",
            ],
            "fix_in_code": [
                "Centralize secret redaction before logs/events are emitted.",
                "Tighten token/session validation and shorten credential exposure windows.",
                "Add automated tests for credential leakage and replay protection.",
            ],
            "improve_later": [
                "Automate secret scanning in CI for app and config changes.",
                "Run periodic credential-rotation drills with recovery playbooks.",
                "Add stronger auth anomaly dashboards and alert thresholds.",
            ],
        }

    if DRILL_STATE["drill_type"] == "pii_exposure":
        return {
            "do_now": [
                "Block the highest-risk data-return paths until filters are verified.",
                "Review recent responses/logs for accidental sensitive field exposure.",
                "Confirm least-privilege access rules on PII-bearing endpoints.",
            ],
            "fix_in_code": [
                "Apply strict response schemas that exclude non-essential sensitive fields.",
                "Add field-level authorization checks before returning client data.",
                "Add regression tests for PII redaction and safe serialization.",
            ],
            "improve_later": [
                "Classify sensitive fields and enforce policy checks in CI.",
                "Introduce privacy-focused threat modeling for new user-data features.",
                "Track data-exposure near misses as a first-class reliability metric.",
            ],
        }

    if DRILL_STATE["drill_type"] == "dependency_api_failure":
        return {
            "do_now": [
                "Stabilize core flows with temporary fallback behavior for upstream outages.",
                "Throttle dependency-heavy features while critical paths recover.",
                "Verify user-facing communication for degraded third-party functionality.",
            ],
            "fix_in_code": [
                "Add strict timeouts, retries with bounds, and circuit breakers for external APIs.",
                "Implement cached/default responses for non-critical dependency calls.",
                "Add integration-failure tests for each critical third-party path.",
            ],
            "improve_later": [
                "Define dependency reliability budgets and escalation runbooks.",
                "Track upstream error propagation to user flows in dashboards.",
                "Add periodic outage simulation for top external integrations.",
            ],
        }

    if DRILL_STATE["drill_type"] == "ai_risk_suite":
        return {
            "do_now": [
                "Address the highest-impact risk from each suite category before release.",
                "Assign owners for credential, data-protection, and dependency resilience fixes.",
                "Re-run the suite after hotfixes to verify risk reduction.",
            ],
            "fix_in_code": [
                "Harden auth/secret handling, including redaction and replay protection.",
                "Enforce PII-safe response contracts and field-level authorization.",
                "Add dependency fallback patterns with bounded retries and circuit breakers.",
            ],
            "improve_later": [
                "Automate this suite in pre-release checks for continuous risk visibility.",
                "Track top-risk trendlines to measure resilience improvement over time.",
                "Expand suite coverage as new features and data paths are introduced.",
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

    action_plan = ollama_json_request(prompt)
    print("[WARROOM backend] Ollama action plan success")
    return {
        "do_now": action_plan["do_now"][:3],
        "fix_in_code": action_plan["fix_in_code"][:3],
        "improve_later": action_plan["improve_later"][:3],
    }


def build_live_interpretation_input() -> dict:
    if DRILL_STATE["drill_type"] == "db_down":
        evidence = DRILL_STATE["evidence"] or build_real_db_down_evidence()
        app_status = "degraded" if evidence.get("error_count", 0) > 0 else "running"
        db_status = "stopped" if DRILL_STATE["db_stop_time"] is not None else "running"
    elif DRILL_STATE["drill_type"] == "latency_spike":
        evidence = DRILL_STATE["evidence"] or build_real_latency_evidence()
        app_status = "degraded" if (
            (evidence.get("p95_latency") or 0) >= 800 or (evidence.get("error_count") or 0) > 0
        ) else "running"
        db_status = "running"
    else:
        evidence = DRILL_STATE["evidence"] or build_evidence(DRILL_STATE["drill_type"] or "db_down")
        app_status = "degraded" if (evidence.get("error_count") or 0) > 0 else "running"
        db_status = "running"

    status_snapshot = {
        "drill_type": DRILL_STATE["drill_type"] or "db_down",
        "status": DRILL_STATE["status"],
        "app_status": app_status,
        "db_status": db_status,
        "success_rate": evidence.get("success_rate"),
        "error_count": evidence.get("error_count"),
        "p95_latency": evidence.get("p95_latency"),
        "first_failure_time": evidence.get("first_failure_time"),
        "mcp_activity": list(DRILL_STATE["mcp_activity"]),
        "timeline": list(DRILL_STATE["timeline"]),
    }
    return status_snapshot


def generate_ollama_live_interpretation(interpretation_input: dict) -> dict:
    print("[WARROOM backend] starting Ollama live interpretation")
    prompt = (
        "You are generating live failure narration for a resilience drill dashboard.\n"
        "Return valid JSON only in this shape: {\"lines\": [\"...\", \"...\", \"...\", \"...\"]}\n"
        "Rules:\n"
        "- maximum 4 lines\n"
        "- plain English\n"
        "- short and crisp\n"
        "- grounded only in the provided signals\n"
        "- do not invent facts, metrics, actions, or systems\n"
        "- connect system behavior to user impact\n"
        "- explain MCP actions in human language\n"
        "- if evidence is weak, keep the lines cautious\n\n"
        f"Signals:\n{json.dumps(interpretation_input, indent=2)}"
    )
    result = ollama_json_request(prompt, timeout=12)
    lines = result.get("lines", [])
    if not isinstance(lines, list):
        raise ValueError("Ollama live interpretation did not return a lines list")
    cleaned_lines = [str(line).strip() for line in lines if str(line).strip()][:4]
    print("[WARROOM backend] Ollama live interpretation success")
    return {"lines": cleaned_lines}


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

    if drill_type == "credential_exposure":
        snapshots = [
            {
                "app_status": "running",
                "db_status": "running",
                "success_rate": 99,
                "error_count": 0,
                "p95_latency": 150,
                "first_failure_time": None,
                "timeline": ["00:00 - Drill started"],
            },
            {
                "app_status": "degraded",
                "db_status": "running",
                "success_rate": 94,
                "error_count": 3,
                "p95_latency": 260,
                "first_failure_time": 3,
                "timeline": [
                    "00:00 - Drill started",
                    "00:03 - credential leak signal detected",
                    "00:04 - risky auth requests observed",
                    "00:10 - Drill complete",
                ],
            },
        ]
        index = min(max((poll_count // 3), 0), len(snapshots) - 1)
        return snapshots[index]

    if drill_type == "pii_exposure":
        snapshots = [
            {
                "app_status": "running",
                "db_status": "running",
                "success_rate": 98,
                "error_count": 1,
                "p95_latency": 180,
                "first_failure_time": 4,
                "timeline": [
                    "00:00 - Drill started",
                    "00:04 - data boundary weakness surfaced",
                    "00:10 - Drill complete",
                ],
            },
        ]
        return snapshots[0]

    if drill_type == "dependency_api_failure":
        snapshots = [
            {
                "app_status": "running",
                "db_status": "running",
                "success_rate": 97,
                "error_count": 1,
                "p95_latency": 210,
                "first_failure_time": None,
                "timeline": ["00:00 - Drill started"],
            },
            {
                "app_status": "degraded",
                "db_status": "running",
                "success_rate": 86,
                "error_count": 11,
                "p95_latency": 620,
                "first_failure_time": 4,
                "timeline": [
                    "00:00 - Drill started",
                    "00:02 - upstream API failure injected",
                    "00:04 - fallback path overloaded",
                    "00:10 - Drill complete",
                ],
            },
        ]
        index = min(max((poll_count // 3), 0), len(snapshots) - 1)
        return snapshots[index]

    if drill_type == "ai_risk_suite":
        snapshots = [
            {
                "app_status": "running",
                "db_status": "running",
                "success_rate": 98,
                "error_count": 1,
                "p95_latency": 180,
                "first_failure_time": None,
                "timeline": [
                    "00:00 - Suite started",
                    "00:01 - Test 1/3 Credential Exposure Risk",
                ],
            },
            {
                "app_status": "degraded",
                "db_status": "running",
                "success_rate": 90,
                "error_count": 8,
                "p95_latency": 440,
                "first_failure_time": 3,
                "timeline": [
                    "00:00 - Suite started",
                    "00:01 - Test 1/3 Credential Exposure Risk",
                    "00:04 - Test 2/3 PII Exposure Risk",
                ],
            },
            {
                "app_status": "degraded",
                "db_status": "running",
                "success_rate": 82,
                "error_count": 17,
                "p95_latency": 760,
                "first_failure_time": 3,
                "timeline": [
                    "00:00 - Suite started",
                    "00:01 - Test 1/3 Credential Exposure Risk",
                    "00:04 - Test 2/3 PII Exposure Risk",
                    "00:07 - Test 3/3 Dependency API Failure",
                    "00:10 - Suite complete",
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

    if drill_type == "credential_exposure":
        return {
            "success_rate": 90,
            "p95_latency": 340,
            "error_count": 6,
            "first_failure_time": 3,
            "likely_cause": (
                "Credential handling controls were weak in simulated auth paths, "
                "increasing account takeover risk under abuse conditions."
            ),
            "suggested_fix": (
                "Mask secrets in logs, enforce secret rotation, and harden auth flows "
                "with strict token/session protections."
            ),
            "summary": "The drill indicates elevated risk of credential misuse and account compromise.",
            "logs": [
                "[auth] suspicious token replay pattern detected",
                "[policy] weak secret-handling path flagged",
                "[metrics] success_rate=90 error_count=6 p95_latency=340",
            ],
            "timeline": [
                "00:00 - Drill started",
                "00:03 - credential leak signal detected",
                "00:05 - risky auth behavior escalated",
                "00:10 - Drill complete",
            ],
        }

    if drill_type == "pii_exposure":
        return {
            "success_rate": 92,
            "p95_latency": 310,
            "error_count": 5,
            "first_failure_time": 4,
            "likely_cause": (
                "Sensitive client data boundaries were insufficient in simulated response and access paths."
            ),
            "suggested_fix": (
                "Apply strict field-level data minimization, access checks, and output filtering "
                "for PII-bearing endpoints."
            ),
            "summary": "The drill indicates data exposure risk in client-facing data flows.",
            "logs": [
                "[api] simulated over-broad response payload detected",
                "[policy] data classification guardrail missing on one route",
                "[metrics] success_rate=92 error_count=5 p95_latency=310",
            ],
            "timeline": [
                "00:00 - Drill started",
                "00:04 - data boundary weakness surfaced",
                "00:10 - Drill complete",
            ],
        }

    if drill_type == "dependency_api_failure":
        return {
            "success_rate": 84,
            "p95_latency": 690,
            "error_count": 13,
            "first_failure_time": 4,
            "likely_cause": (
                "A simulated third-party API outage propagated into critical app flows "
                "because fallback handling was limited."
            ),
            "suggested_fix": (
                "Add cached fallback responses, strict timeout budgets, and circuit breakers "
                "for external API dependencies."
            ),
            "summary": "The drill indicates external dependency failure can disrupt key user journeys.",
            "logs": [
                "[dependency] upstream API failure injected",
                "[app] fallback path saturated",
                "[metrics] success_rate=84 error_count=13 p95_latency=690",
            ],
            "timeline": [
                "00:00 - Drill started",
                "00:02 - upstream API failure injected",
                "00:04 - fallback path overloaded",
                "00:10 - Drill complete",
            ],
        }

    if drill_type == "ai_risk_suite":
        return {
            "success_rate": 82,
            "p95_latency": 760,
            "error_count": 17,
            "first_failure_time": 3,
            "likely_cause": (
                "The short top-risk suite surfaced multiple resilience gaps across credentials, "
                "PII controls, and third-party dependency handling."
            ),
            "suggested_fix": (
                "Prioritize credential and data-protection hardening first, then add stronger "
                "dependency fallback patterns for third-party integrations."
            ),
            "summary": "The AI-selected risk suite identified multiple high-impact weaknesses across the app.",
            "logs": [
                "[suite] test 1/3 credential exposure completed",
                "[suite] test 2/3 pii exposure completed",
                "[suite] test 3/3 dependency API failure completed",
                "[metrics] success_rate=82 error_count=17 p95_latency=760",
            ],
            "timeline": [
                "00:00 - Suite started",
                "00:01 - Test 1/3 Credential Exposure Risk",
                "00:04 - Test 2/3 PII Exposure Risk",
                "00:07 - Test 3/3 Dependency API Failure",
                "00:10 - Suite complete",
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
    mentions_uncertain = any(
        term in lower_fear
        for term in [
            "not sure",
            "dont know",
            "don't know",
            "do not know",
            "decide for me",
            "anything",
            "top vulnerabilities",
        ]
    )
    mentions_database = "database" in lower_fear or "db" in lower_fear
    mentions_down = "down" in lower_fear or "fail" in lower_fear
    mentions_latency = "slow" in lower_fear or "latency" in lower_fear
    mentions_flood = (
        "flood" in lower_fear
        or "traffic" in lower_fear
        or "requests" in lower_fear
    )
    mentions_credentials = any(
        term in lower_fear
        for term in ["password", "credential", "secret", "token leak", "account takeover"]
    )
    mentions_pii = any(
        term in lower_fear
        for term in ["client data", "pii", "personal data", "data leak", "privacy"]
    )
    mentions_dependency = any(
        term in lower_fear
        for term in ["third party", "dependency", "external api", "upstream api"]
    )

    if mentions_uncertain:
        return "ai_risk_suite"
    if mentions_credentials:
        return "credential_exposure"
    if mentions_pii:
        return "pii_exposure"
    if mentions_dependency:
        return "dependency_api_failure"

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

    if is_remediated_drill(request.drill_type):
        DRILL_STATE["timeline"] = ["00:00 - Verification run started"]
        DRILL_STATE["mcp_activity"] = [
            "Remediation profile loaded from pasted prompt",
            "Running verification simulation for resolved state",
        ]
        DRILL_STATE["evidence"] = build_resolved_evidence(request.drill_type)
        print(
            f"[WARROOM backend] verification start "
            f"drill_id={drill_id} drill_type={request.drill_type}"
        )
        return {
            "drill_id": drill_id,
            "status": "started",
            "verification_mode": True,
        }

    if request.drill_type in {
        "db_down",
        "latency_spike",
        "credential_exposure",
        "pii_exposure",
        "dependency_api_failure",
        "ai_risk_suite",
    }:
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
        if request.drill_type not in {"db_down", "latency_spike"}:
            DRILL_STATE["evidence"] = build_evidence(request.drill_type)
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

    if is_remediated_drill(DRILL_STATE["drill_type"]):
        snapshot = build_resolved_snapshot(
            DRILL_STATE["drill_type"] or "db_down",
            DRILL_STATE["poll_count"],
        )
        if DRILL_STATE["status"] == "complete":
            DRILL_STATE["evidence"] = build_resolved_evidence(
                DRILL_STATE["drill_type"] or "db_down"
            )
    elif DRILL_STATE["drill_type"] == "db_down":
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

    if is_remediated_drill(DRILL_STATE["drill_type"]):
        return build_resolved_evidence(DRILL_STATE["drill_type"] or "db_down")

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


@app.get("/drill/live-interpretation")
def drill_live_interpretation():
    print(
        f"[WARROOM backend] GET /drill/live-interpretation "
        f"drill_id={DRILL_STATE['drill_id']} status={DRILL_STATE['status']}"
    )

    if not DRILL_STATE["drill_id"] or DRILL_STATE["status"] == "idle":
        return {"lines": []}

    interpretation_input = build_live_interpretation_input()
    try:
        return generate_ollama_live_interpretation(interpretation_input)
    except Exception as exc:
        print(f"[WARROOM backend] live interpretation fallback: {exc}")
        return {"lines": []}


@app.get("/drill/action-plan")
def drill_action_plan():
    print(
        f"[WARROOM backend] GET /drill/action-plan "
        f"drill_id={DRILL_STATE['drill_id']} status={DRILL_STATE['status']}"
    )

    if is_remediated_drill(DRILL_STATE["drill_type"]):
        return {
            "do_now": [
                "Keep the remediation deployed and monitor for regression.",
                "Run one additional verification cycle before release.",
                "Document the final fix and owner handoff.",
            ],
            "fix_in_code": [
                "Preserve fallback and resilience controls added by remediation.",
                "Keep verification tests in CI to guard against regressions.",
                "Harden alerting for early detection if issue reappears.",
            ],
            "improve_later": [
                "Schedule recurring chaos and risk drills for this path.",
                "Track resilience trend metrics over future releases.",
                "Expand this remediation pattern to similar service paths.",
            ],
        }

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


@app.get("/remediation/prompt")
def remediation_prompt(drill_type: str | None = None):
    effective_drill_type = drill_type or DRILL_STATE["drill_type"] or "db_down"
    evidence = DRILL_STATE["evidence"] or build_evidence(effective_drill_type)

    action_plan_input = {
        "drill_type": effective_drill_type,
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
        action_plan = generate_ollama_action_plan(action_plan_input)
    except Exception:
        action_plan = build_fallback_action_plan(evidence)

    return {
        "drill_type": effective_drill_type,
        "prompt": build_remediation_prompt_template(
            effective_drill_type,
            evidence,
            action_plan,
        ),
    }


@app.post("/remediation/apply")
def remediation_apply(payload: RemediationApplyRequest):
    effective_drill_type = payload.drill_type or DRILL_STATE["drill_type"] or "db_down"
    normalized_prompt = payload.prompt.strip()
    if not normalized_prompt:
        raise HTTPException(status_code=400, detail="Remediation prompt cannot be empty.")

    REMEDIATION_STATE["applied"][effective_drill_type] = True
    REMEDIATION_STATE["last_prompt"] = normalized_prompt
    REMEDIATION_STATE["last_drill_type"] = effective_drill_type
    REMEDIATION_STATE["applied_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    return {
        "status": "applied",
        "drill_type": effective_drill_type,
        "applied_at": REMEDIATION_STATE["applied_at"],
        "message": "Remediation prompt recorded. Run verification re-test to confirm no critical errors.",
    }


@app.get("/remediation/status")
def remediation_status(drill_type: str | None = None):
    effective_drill_type = drill_type or DRILL_STATE["drill_type"] or REMEDIATION_STATE["last_drill_type"]
    return {
        "drill_type": effective_drill_type,
        "is_applied": is_remediated_drill(effective_drill_type),
        "applied_at": REMEDIATION_STATE["applied_at"],
    }


@app.post("/remediation/verify")
def remediation_verify(payload: RemediationVerifyRequest):
    effective_drill_type = payload.drill_type or DRILL_STATE["drill_type"] or REMEDIATION_STATE["last_drill_type"] or "db_down"
    is_applied = is_remediated_drill(effective_drill_type)
    verification = build_resolved_evidence(effective_drill_type) if is_applied else build_evidence(effective_drill_type)
    resolved = bool(is_applied)

    return {
        "drill_type": effective_drill_type,
        "resolved": resolved,
        "status": "pass" if resolved else "needs_more_work",
        "message": (
            "Verification predicts critical issues are cleared for this drill scenario."
            if resolved
            else "No remediation prompt applied for this drill type yet."
        ),
        "metrics": {
            "success_rate": verification.get("success_rate"),
            "error_count": verification.get("error_count"),
            "p95_latency": verification.get("p95_latency"),
            "first_failure_time": verification.get("first_failure_time"),
        },
    }


@app.post("/drill/reset")
def reset_drill():
    print(f"[WARROOM backend] POST /drill/reset drill_id={DRILL_STATE['drill_id']}")

    if DRILL_STATE["drill_type"] in {
        "db_down",
        "latency_spike",
        "credential_exposure",
        "pii_exposure",
        "dependency_api_failure",
        "ai_risk_suite",
    }:
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
