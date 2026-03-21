const DRILL_CONFIG = {
  db_down: {
    label: "DB Down",
    targetService: "warroom-db",
    duration: "60 seconds",
    impact: "Checkout requests may return 5xx errors"
  },
  latency_spike: {
    label: "Latency Spike",
    targetService: "warroom-db via toxiproxy",
    duration: "60 seconds",
    impact: "Responses may slow down or time out"
  },
  request_flood: {
    label: "Request Flood",
    targetService: "warroom-app",
    duration: "20 seconds",
    impact: "Success rate may drop under load"
  }
};

const SIMULATION_DURATION_MS = 10000;
const PROGRESS_TICK_MS = 100;

const DEFAULT_BATTLE_STATE = {
  appStatus: "running",
  dbStatus: "running",
  successRate: "100%",
  errorCount: "0",
  p95Latency: "120ms",
  firstFailure: "--",
  progressPercent: 0,
  statusText: "Drill in progress...",
  timeline: [
    { time: "00:00", text: "Drill started" }
  ]
};

const DB_DOWN_SIMULATION_STEPS = [
  {
    at: 2000,
    label: "db_down_impact_started",
    apply(state) {
      state.dbStatus = "stopped";
      state.successRate = "98%";
      state.p95Latency = "180ms";
      state.timeline.push({ time: "00:02", text: "warroom-db stopped responding" });
    }
  },
  {
    at: 3000,
    label: "first_failure_observed",
    apply(state) {
      state.firstFailure = "checkout-api";
      state.errorCount = "4";
      state.successRate = "92%";
      state.p95Latency = "280ms";
      state.timeline.push({ time: "00:03", text: "First checkout failure detected" });
    }
  },
  {
    at: 4000,
    label: "checkout_connection_refused",
    apply(state) {
      state.errorCount = "9";
      state.successRate = "84%";
      state.p95Latency = "420ms";
      state.timeline.push({ time: "00:04", text: "checkout failed: database connection refused" });
    }
  },
  {
    at: 5000,
    label: "error_budget_burn",
    apply(state) {
      state.errorCount = "16";
      state.successRate = "76%";
      state.p95Latency = "640ms";
      state.timeline.push({ time: "00:05", text: "5xx errors increasing across checkout" });
    }
  },
  {
    at: 7000,
    label: "customer_impact_visible",
    apply(state) {
      state.appStatus = "degraded";
      state.errorCount = "29";
      state.successRate = "61%";
      state.p95Latency = "910ms";
      state.timeline.push({ time: "00:07", text: "warroom-app degraded under dependency failure" });
    }
  },
  {
    at: 9000,
    label: "drill_near_completion",
    apply(state) {
      state.errorCount = "34";
      state.successRate = "58%";
      state.p95Latency = "1020ms";
      state.timeline.push({ time: "00:09", text: "Drill objectives captured, wrapping up" });
    }
  }
];

let currentPlan = null;
let currentDrillId = null;
let battleState = cloneBattleState(DEFAULT_BATTLE_STATE);
let verdictState = null;
let simulationTimeoutIds = [];
let progressIntervalId = null;
let drillStatusPollingId = null;
let simulationActive = false;

function cloneBattleState(state) {
  return {
    ...state,
    timeline: state.timeline.map((event) => ({ ...event }))
  };
}

function setFear(text) {
  document.getElementById("fearInput").value = text;
  console.log("[WARROOM] fear preset selected:", text);
}

async function classifyFear(fear) {
  console.log("[WARROOM] classification request started", { fear });

  const response = await fetch("http://127.0.0.1:8000/classify", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ fear })
  });

  if (!response.ok) {
    throw new Error(`Classification request failed with status ${response.status}`);
  }

  const data = await response.json();

  console.log("[WARROOM] classification response received", data);

  return {
    drillType: data.drill_type,
    label: data.label,
    targetService: data.target_service,
    duration: data.duration,
    impact: data.expected_impact
  };
}

async function startDrillRequest(drillType) {
  console.log("[WARROOM] drill start request started", { drillType });

  const response = await fetch("http://127.0.0.1:8000/drill/start", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ drill_type: drillType })
  });

  if (!response.ok) {
    throw new Error(`Drill start request failed with status ${response.status}`);
  }

  const data = await response.json();

  console.log("[WARROOM] drill start request succeeded", data);

  return data;
}

function showScreen(screenToShowId) {
  const screens = Array.from(document.querySelectorAll(".screen-panel"));

  screens.forEach((screen) => {
    screen.classList.toggle("hidden", screen.id !== screenToShowId);
  });

  console.log("[WARROOM] screen transition:", screenToShowId);
}

function populateApprovalScreen(plan) {
  document.getElementById("drillType").textContent = plan.label;
  document.getElementById("targetService").textContent = plan.targetService;
  document.getElementById("duration").textContent = plan.duration;
  document.getElementById("impact").textContent = plan.impact;
}

async function runDrill() {
  const fear = document.getElementById("fearInput").value.trim();

  if (!fear) {
    alert("Please enter a fear");
    return;
  }

  try {
    const plan = await classifyFear(fear);
    currentPlan = plan;

    populateApprovalScreen(plan);
    showScreen("screen2");
  } catch (error) {
    console.error("[WARROOM] classification request failed", error);
    alert("Could not classify fear. Please make sure the backend is running.");
  }
}

function goBack() {
  console.log("[WARROOM] cancel clicked, returning to input screen");
  showScreen("screen1");
}

function clearSimulationTimers() {
  simulationTimeoutIds.forEach((timeoutId) => clearTimeout(timeoutId));
  simulationTimeoutIds = [];

  if (progressIntervalId) {
    clearInterval(progressIntervalId);
    progressIntervalId = null;
  }
}

function stopDrillStatusPolling() {
  if (drillStatusPollingId) {
    clearInterval(drillStatusPollingId);
    drillStatusPollingId = null;
    console.log("[WARROOM] polling stopped");
  }
}

function resetEvidencePanel() {
  const evidencePanel = document.getElementById("evidencePanel");
  const evidenceToggle = document.getElementById("evidenceToggle");

  if (!evidencePanel || !evidenceToggle) {
    return;
  }

  evidencePanel.classList.add("hidden");
  evidenceToggle.textContent = "Show Evidence";
}

function resetBattleState() {
  clearSimulationTimers();
  stopDrillStatusPolling();
  simulationActive = false;
  battleState = cloneBattleState(DEFAULT_BATTLE_STATE);
  verdictState = null;
  renderBattleState();
  resetEvidencePanel();
  console.log("[WARROOM] battle state reset");
}

function setStatusAppearance(servicePrefix, status) {
  const dot = document.getElementById(`${servicePrefix}StatusDot`);
  const text = document.getElementById(`${servicePrefix}StatusText`);
  const pill = document.getElementById(`${servicePrefix}StatusPill`);

  dot.className = "status-dot";
  pill.className = "status-pill";

  if (status === "stopped") {
    dot.classList.add("status-stopped");
    pill.classList.add("status-pill-stopped");
  } else if (status === "degraded") {
    dot.classList.add("status-degraded");
    pill.classList.add("status-pill-degraded");
  } else {
    dot.classList.add("status-running");
    pill.classList.add("status-pill-running");
  }

  text.textContent = status;
}

function renderTimeline(events) {
  const timelineList = document.getElementById("timelineList");
  timelineList.innerHTML = events
    .map((event) => (
      `<div class="timeline-item">
        <span class="timeline-time">${event.time}</span>
        <span class="timeline-text">${event.text}</span>
      </div>`
    ))
    .join("");
}

function renderBattleState() {
  setStatusAppearance("app", battleState.appStatus);
  setStatusAppearance("db", battleState.dbStatus);

  document.getElementById("successRate").textContent = battleState.successRate;
  document.getElementById("errorCount").textContent = battleState.errorCount;
  document.getElementById("p95Latency").textContent = battleState.p95Latency;
  document.getElementById("firstFailure").textContent = battleState.firstFailure;
  document.getElementById("progressBar").style.width = `${battleState.progressPercent}%`;
  document.getElementById("drillStatusText").textContent = battleState.statusText;

  renderTimeline(battleState.timeline);
}

function applyDrillStatus(statusData) {
  const timelineEvents = statusData.timeline.map((entry) => {
    const [time, ...rest] = entry.split(" - ");
    return {
      time,
      text: rest.join(" - ")
    };
  });

  battleState.appStatus = statusData.app_status;
  battleState.dbStatus = statusData.db_status;
  battleState.successRate = `${statusData.success_rate}%`;
  battleState.errorCount = String(statusData.error_count);
  battleState.p95Latency = `${statusData.p95_latency}ms`;
  battleState.firstFailure = statusData.first_failure_time === null
    ? "--"
    : `00:${String(statusData.first_failure_time).padStart(2, "0")}`;
  battleState.timeline = timelineEvents;
  battleState.progressPercent = statusData.status === "complete"
    ? 100
    : Math.min(timelineEvents.length * 25, 90);
  battleState.statusText = statusData.status === "complete"
    ? "Drill complete. Preparing verdict..."
    : "Drill in progress...";

  renderBattleState();
}

async function fetchDrillEvidence() {
  console.log("[WARROOM] evidence fetch start");

  const response = await fetch("http://127.0.0.1:8000/drill/evidence");

  if (!response.ok) {
    throw new Error(`Drill evidence request failed with status ${response.status}`);
  }

  const data = await response.json();

  console.log("[WARROOM] evidence fetch success", data);

  return data;
}

async function resetDrillRequest() {
  console.log("[WARROOM] reset request start");

  const response = await fetch("http://127.0.0.1:8000/drill/reset", {
    method: "POST"
  });

  if (!response.ok) {
    throw new Error(`Drill reset request failed with status ${response.status}`);
  }

  const data = await response.json();

  console.log("[WARROOM] reset success", data);

  return data;
}

function buildVerdictState(evidenceData) {
  const timelineLines = battleState.timeline.map((event) => `${event.time} - ${event.text}`);
  const firstFailure = evidenceData.first_failure_time === null
    ? "--"
    : `00:${String(evidenceData.first_failure_time).padStart(2, "0")}`;

  return {
    result: "FAIL",
    summary: "The system did not handle the dependency failure safely.",
    successRate: `${evidenceData.success_rate}%`,
    p95Latency: `${evidenceData.p95_latency}ms`,
    errorCount: String(evidenceData.error_count),
    firstFailure,
    likelyCause: evidenceData.likely_cause,
    suggestedFix: evidenceData.suggested_fix,
    evidence: {
      timelineLines,
      logLines: evidenceData.logs,
      metricsSnapshot: [
        `success_rate: ${evidenceData.success_rate}%`,
        `p95_latency: ${evidenceData.p95_latency}ms`,
        `error_count: ${evidenceData.error_count}`,
        `first_failure: ${firstFailure}`
      ]
    }
  };
}

function populateVerdictScreen() {
  if (!verdictState) {
    return;
  }

  document.getElementById("verdictBadge").textContent = verdictState.result;
  document.getElementById("verdictSummary").textContent = verdictState.summary;
  document.getElementById("verdictSuccessRate").textContent = verdictState.successRate;
  document.getElementById("verdictP95Latency").textContent = verdictState.p95Latency;
  document.getElementById("verdictErrorCount").textContent = verdictState.errorCount;
  document.getElementById("verdictFirstFailure").textContent = verdictState.firstFailure;
  document.getElementById("likelyCause").textContent = verdictState.likelyCause;
  document.getElementById("suggestedFix").textContent = verdictState.suggestedFix;
  document.getElementById("evidenceTimeline").textContent = verdictState.evidence.timelineLines.join("\n");
  document.getElementById("evidenceLogs").textContent = verdictState.evidence.logLines.join("\n");
  document.getElementById("evidenceMetrics").textContent = verdictState.evidence.metricsSnapshot.join("\n");
  resetEvidencePanel();
}

async function showVerdictScreen() {
  try {
    const evidenceData = await fetchDrillEvidence();
    verdictState = buildVerdictState(evidenceData);
    populateVerdictScreen();
    showScreen("screen4");
    console.log("[WARROOM] transition to verdict screen");
  } catch (error) {
    console.error("[WARROOM] evidence fetch failure", error);
    alert("Could not load verdict evidence. Please make sure the backend is running.");
  }
}

async function fetchDrillStatus() {
  const response = await fetch("http://127.0.0.1:8000/drill/status");

  if (!response.ok) {
    throw new Error(`Drill status request failed with status ${response.status}`);
  }

  return response.json();
}

async function pollDrillStatus() {
  try {
    const statusData = await fetchDrillStatus();

    console.log("[WARROOM] polling response received", statusData);

    applyDrillStatus(statusData);

    if (statusData.status === "complete") {
      stopDrillStatusPolling();
      await showVerdictScreen();
    }
  } catch (error) {
    console.error("[WARROOM] drill status polling failed", error);
    stopDrillStatusPolling();
    alert("Could not load drill status. Please make sure the backend is running.");
  }
}

function startDrillStatusPolling() {
  if (drillStatusPollingId) {
    return;
  }

  resetBattleState();
  showScreen("screen3");
  renderBattleState();

  console.log("[WARROOM] polling started", {
    drillId: currentDrillId,
    drillType: currentPlan ? currentPlan.drillType : "db_down"
  });

  pollDrillStatus();
  drillStatusPollingId = window.setInterval(pollDrillStatus, 1000);
}

async function approveRun() {
  if (!currentPlan) {
    alert("Could not start drill. Please make sure the backend is running.");
    return;
  }

  try {
    const data = await startDrillRequest(currentPlan.drillType);
    currentDrillId = data.drill_id;

    console.log("[WARROOM] drill started", {
      drillId: currentDrillId,
      drillType: currentPlan.drillType
    });

    startDrillStatusPolling();
  } catch (error) {
    console.error("[WARROOM] drill start request failed", error);
    alert("Could not start drill. Please make sure the backend is running.");
  }
}

function abortDrill() {
  console.log("[WARROOM] abort clicked, stopping drill");
  currentDrillId = null;
  resetBattleState();
  showScreen("screen1");
}

function toggleEvidence() {
  const evidencePanel = document.getElementById("evidencePanel");
  const evidenceToggle = document.getElementById("evidenceToggle");
  const isHidden = evidencePanel.classList.contains("hidden");

  evidencePanel.classList.toggle("hidden", !isHidden);
  evidenceToggle.textContent = isHidden ? "Hide Evidence" : "Show Evidence";

  console.log(`[WARROOM] evidence panel ${isHidden ? "expanded" : "collapsed"}`);
}

async function resetToStart() {
  try {
    await resetDrillRequest();
    currentDrillId = null;
    currentPlan = null;
    resetBattleState();
    showScreen("screen1");
  } catch (error) {
    console.error("[WARROOM] reset failure", error);
    alert("Could not reset drill. Please make sure the backend is running.");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  resetBattleState();
  showScreen("screen1");
  console.log("[WARROOM] initialized");
});
