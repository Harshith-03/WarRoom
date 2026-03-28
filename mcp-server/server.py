import subprocess
import time
from typing import Literal

import requests
from fastapi import FastAPI, HTTPException
from fastmcp import FastMCP
from pydantic import BaseModel


APP_BASE_URL = "http://127.0.0.1:5001"
TOXIPROXY_BASE_URL = "http://127.0.0.1:8474"
DB_PROXY_NAME = "warroom-db-proxy"

app = FastAPI(title="WARROOM MCP Server")
mcp = FastMCP("WARROOM MCP")

MCP_STATE = {
    "drill_type": None,
    "container": None,
    "proxy": None,
    "last_action": None,
    "activity": [],
}


class RunDrillInput(BaseModel):
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


class ResetInput(BaseModel):
    drill_id: str | None = None


class EvidenceInput(BaseModel):
    drill_id: str


def run_podman_command(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["podman", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def record_activity(message: str) -> None:
    MCP_STATE["activity"].append(message)
    MCP_STATE["activity"] = MCP_STATE["activity"][-8:]


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
    print(f"[WARROOM MCP] resolved container={container_name}")
    record_activity(f"MCP resolved {container_name}")
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
    print("[WARROOM MCP] podman stop executed")
    record_activity("MCP executed Podman stop")


def start_container(container_name: str) -> None:
    result = run_podman_command("start", container_name)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Could not start container {container_name}: {result.stderr.strip()}",
        )
    print("[WARROOM MCP] podman start executed")
    record_activity("MCP executed Podman start")


def wait_for_demo_health(timeout_seconds: int = 15) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            response = requests.get(f"{APP_BASE_URL}/health", timeout=2)
            if 200 <= response.status_code < 400:
                return
        except requests.RequestException:
            pass
        time.sleep(1)

    raise HTTPException(
        status_code=500,
        detail="Demo app health endpoint did not recover after MCP reset.",
    )


def resolve_db_proxy_name() -> str:
    try:
        response = requests.get(f"{TOXIPROXY_BASE_URL}/proxies", timeout=5)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not inspect Toxiproxy proxies: {exc}",
        ) from exc

    proxies = response.json()
    if isinstance(proxies, dict) and DB_PROXY_NAME in proxies:
        print(f"[WARROOM MCP] resolved proxy={DB_PROXY_NAME}")
        record_activity(f"MCP resolved {DB_PROXY_NAME}")
        return DB_PROXY_NAME

    raise HTTPException(
        status_code=500,
        detail=f"Could not find Toxiproxy proxy named {DB_PROXY_NAME}.",
    )


def inject_latency_toxic(proxy_name: str, latency_ms: int = 800) -> None:
    try:
        response = requests.post(
            f"{TOXIPROXY_BASE_URL}/proxies/{proxy_name}/toxics",
            json={
                "name": "latency",
                "type": "latency",
                "attributes": {
                    "latency": latency_ms,
                },
            },
            timeout=5,
        )
        if response.status_code == 409:
            delete_latency_toxic(proxy_name, raise_if_missing=False)
            response = requests.post(
                f"{TOXIPROXY_BASE_URL}/proxies/{proxy_name}/toxics",
                json={
                    "name": "latency",
                    "type": "latency",
                    "attributes": {
                        "latency": latency_ms,
                    },
                },
                timeout=5,
            )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not inject latency toxic: {exc}",
        ) from exc

    print(f"[WARROOM MCP] injected {latency_ms}ms latency")
    record_activity(f"MCP injected {latency_ms}ms latency")


def delete_latency_toxic(proxy_name: str, raise_if_missing: bool = True) -> None:
    try:
        response = requests.delete(
            f"{TOXIPROXY_BASE_URL}/proxies/{proxy_name}/toxics/latency",
            timeout=5,
        )
        if response.status_code == 404 and not raise_if_missing:
            return
        response.raise_for_status()
    except requests.RequestException as exc:
        if not raise_if_missing and getattr(exc, "response", None) is not None:
            if exc.response.status_code == 404:
                return
        raise HTTPException(
            status_code=500,
            detail=f"Could not remove latency toxic: {exc}",
        ) from exc

    print("[WARROOM MCP] removed latency toxic")
    record_activity("MCP removed latency toxic")


def run_drill_impl(
    drill_type: Literal[
        "db_down",
        "latency_spike",
        "request_flood",
        "credential_exposure",
        "pii_exposure",
        "dependency_api_failure",
        "ai_risk_suite",
    ],
    duration: str | None = None,
    intensity: str | None = None,
) -> dict:
    print(f"[WARROOM MCP] run_drill called drill_type={drill_type}")
    MCP_STATE["activity"] = []
    record_activity(f"MCP received run_drill({drill_type})")

    MCP_STATE["drill_type"] = drill_type

    if drill_type == "db_down":
        container_name = resolve_db_container_name()
        MCP_STATE["container"] = container_name
        stop_container(container_name)
        MCP_STATE["last_action"] = "stopped database container"
        return {
            "ok": True,
            "drill_type": drill_type,
            "action": "stopped database container",
            "container": container_name,
            "mcp_activity": list(MCP_STATE["activity"]),
            "duration": duration,
            "intensity": intensity,
        }

    if drill_type == "latency_spike":
        proxy_name = resolve_db_proxy_name()
        MCP_STATE["proxy"] = proxy_name
        inject_latency_toxic(proxy_name, latency_ms=800)
        record_activity("MCP monitoring active")
        MCP_STATE["last_action"] = "injected database latency"
        return {
            "ok": True,
            "drill_type": drill_type,
            "action": "injected database latency",
            "proxy": proxy_name,
            "latency_ms": 800,
            "mcp_activity": list(MCP_STATE["activity"]),
            "duration": duration,
            "intensity": intensity,
        }

    if drill_type == "credential_exposure":
        record_activity("MCP enabled credential exposure simulation")
        record_activity("MCP tracking auth and secret handling signals")
        MCP_STATE["last_action"] = "simulated credential exposure risk"
        return {
            "ok": True,
            "drill_type": drill_type,
            "action": "simulated credential exposure risk",
            "container": None,
            "mcp_activity": list(MCP_STATE["activity"]),
            "duration": duration,
            "intensity": intensity,
        }

    if drill_type == "pii_exposure":
        record_activity("MCP enabled PII exposure simulation")
        record_activity("MCP tracking sensitive data boundary events")
        MCP_STATE["last_action"] = "simulated pii exposure risk"
        return {
            "ok": True,
            "drill_type": drill_type,
            "action": "simulated pii exposure risk",
            "container": None,
            "mcp_activity": list(MCP_STATE["activity"]),
            "duration": duration,
            "intensity": intensity,
        }

    if drill_type == "dependency_api_failure":
        record_activity("MCP enabled dependency API failure simulation")
        record_activity("MCP observing fallback behavior under upstream outage")
        MCP_STATE["last_action"] = "simulated dependency api failure"
        return {
            "ok": True,
            "drill_type": drill_type,
            "action": "simulated dependency api failure",
            "container": None,
            "mcp_activity": list(MCP_STATE["activity"]),
            "duration": duration,
            "intensity": intensity,
        }

    if drill_type == "ai_risk_suite":
        record_activity("MCP launched AI risk suite orchestration")
        record_activity("MCP scheduled tests: credentials, pii, dependency")
        MCP_STATE["last_action"] = "simulated ai risk suite"
        return {
            "ok": True,
            "drill_type": drill_type,
            "action": "simulated ai risk suite",
            "container": None,
            "mcp_activity": list(MCP_STATE["activity"]),
            "duration": duration,
            "intensity": intensity,
        }

    MCP_STATE["last_action"] = f"stubbed {drill_type}"
    return {
        "ok": True,
        "drill_type": drill_type,
        "action": f"stubbed {drill_type}",
        "container": None,
        "mcp_activity": list(MCP_STATE["activity"]),
        "duration": duration,
        "intensity": intensity,
    }


def reset_impl(drill_id: str | None = None) -> dict:
    print(f"[WARROOM MCP] reset called drill_id={drill_id}")
    record_activity("MCP reset called")

    if MCP_STATE["drill_type"] == "db_down":
        container_name = MCP_STATE["container"] or resolve_db_container_name()
        if not container_is_running(container_name):
            start_container(container_name)
        wait_for_demo_health()
        MCP_STATE["last_action"] = "environment reset"
        return {
            "ok": True,
            "action": "environment reset",
            "container": container_name,
            "mcp_activity": list(MCP_STATE["activity"]),
        }

    if MCP_STATE["drill_type"] == "latency_spike":
        proxy_name = MCP_STATE["proxy"] or resolve_db_proxy_name()
        delete_latency_toxic(proxy_name, raise_if_missing=False)
        MCP_STATE["last_action"] = "environment reset"
        return {
            "ok": True,
            "action": "environment reset",
            "container": MCP_STATE["container"],
            "proxy": proxy_name,
            "mcp_activity": list(MCP_STATE["activity"]),
        }

    MCP_STATE["last_action"] = "environment reset"
    return {
        "ok": True,
        "action": "environment reset",
        "container": MCP_STATE["container"],
        "mcp_activity": list(MCP_STATE["activity"]),
    }


def get_evidence_impl(drill_id: str) -> dict:
    print(f"[WARROOM MCP] get_evidence called drill_id={drill_id}")
    return {
        "ok": True,
        "drill_id": drill_id,
        "action": "evidence owned by backend",
        "drill_type": MCP_STATE["drill_type"],
        "container": MCP_STATE["container"],
        "mcp_activity": list(MCP_STATE["activity"]),
    }


@mcp.tool()
def run_drill(
    drill_type: Literal[
        "db_down",
        "latency_spike",
        "request_flood",
        "credential_exposure",
        "pii_exposure",
        "dependency_api_failure",
        "ai_risk_suite",
    ],
    duration: str | None = None,
    intensity: str | None = None,
) -> dict:
    return run_drill_impl(drill_type, duration, intensity)


@mcp.tool()
def reset(drill_id: str | None = None) -> dict:
    return reset_impl(drill_id)


@mcp.tool()
def get_evidence(drill_id: str) -> dict:
    return get_evidence_impl(drill_id)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "warroom-mcp"}


@app.post("/tools/run_drill")
def run_drill_endpoint(payload: RunDrillInput) -> dict:
    return run_drill_impl(payload.drill_type, payload.duration, payload.intensity)


@app.post("/tools/reset")
def reset_endpoint(payload: ResetInput) -> dict:
    return reset_impl(payload.drill_id)


@app.post("/tools/get_evidence")
def get_evidence_endpoint(payload: EvidenceInput) -> dict:
    return get_evidence_impl(payload.drill_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9100)
