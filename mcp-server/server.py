import subprocess
import time
from typing import Literal

import requests
from fastapi import FastAPI, HTTPException
from fastmcp import FastMCP
from pydantic import BaseModel


APP_BASE_URL = "http://127.0.0.1:5001"

app = FastAPI(title="WARROOM MCP Server")
mcp = FastMCP("WARROOM MCP")

MCP_STATE = {
    "drill_type": None,
    "container": None,
    "last_action": None,
    "activity": [],
}


class RunDrillInput(BaseModel):
    drill_type: Literal["db_down", "latency_spike", "request_flood"]
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


def run_drill_impl(
    drill_type: Literal["db_down", "latency_spike", "request_flood"],
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
    drill_type: Literal["db_down", "latency_spike", "request_flood"],
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
