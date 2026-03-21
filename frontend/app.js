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
  mcpActivity: [],
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
let actionPlanState = null;
let simulationTimeoutIds = [];
let progressIntervalId = null;
let drillStatusPollingId = null;
let simulationActive = false;
let resetInProgress = false;
let battleStartedAt = null;
let verdictTransitionInFlight = false;
let battleCompleted = false;

function cloneBattleState(state) {
  return {
    ...state,
    timeline: state.timeline.map((event) => ({ ...event })),
    mcpActivity: [...state.mcpActivity]
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

async function startDrillRequest(drillType, duration, intensity) {
  console.log("[WARROOM] drill start request started", { drillType, duration, intensity });

  const response = await fetch("http://127.0.0.1:8000/drill/start", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      drill_type: drillType,
      duration,
      intensity
    })
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

function updateApprovalHelper() {
  const duration = document.getElementById("durationSelect").value;
  const intensity = document.getElementById("intensitySelect").value;
  document.getElementById("approvalHelper").textContent =
    `This drill will run for ${duration} at ${intensity} intensity.`;
}

function populateApprovalScreen(plan) {
  document.getElementById("drillType").textContent = plan.label;
  document.getElementById("targetService").textContent = plan.targetService;
  document.getElementById("durationSelect").value = plan.duration;
  document.getElementById("intensitySelect").value = "Medium";
  document.getElementById("impact").textContent = plan.impact;
  updateApprovalHelper();
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
  battleStartedAt = null;
  verdictTransitionInFlight = false;
  battleCompleted = false;
  battleState = cloneBattleState(DEFAULT_BATTLE_STATE);
  verdictState = null;
  actionPlanState = null;
  renderBattleState();
  setViewVerdictButtonVisible(false);
  resetEvidencePanel();
  console.log("[WARROOM] battle state reset");
}

function setViewVerdictButtonVisible(isVisible) {
  const viewVerdictButton = document.getElementById("viewVerdictButton");
  if (!viewVerdictButton) {
    return;
  }

  viewVerdictButton.classList.toggle("hidden", !isVisible);
}

function formatServiceStatusLabel(status) {
  const successRateValue = Number.parseInt(battleState.successRate, 10);

  if (status === "stopped") {
    return "Offline";
  }

  if (status === "degraded") {
    if (!Number.isNaN(successRateValue) && successRateValue === 0) {
      return "Not working";
    }

    if (!Number.isNaN(successRateValue) && successRateValue < 50) {
      return "Mostly failing";
    }

    return "Partially working";
  }

  return "Running";
}

function humanizeTimelineText(text) {
  if (/drill started/i.test(text)) {
    return "Simulation started";
  }

  if (/warroom-db stopped/i.test(text)) {
    return "Database went offline";
  }

  if (/first 5xx response/i.test(text)) {
    return "First checkout error appeared";
  }

  if (/error rate increasing/i.test(text) || /5xx errors increasing/i.test(text)) {
    return "Failures increased";
  }

  if (/drill complete/i.test(text)) {
    return "Simulation completed";
  }

  return text;
}

function buildBattleSummaryLines() {
  const successRateValue = Number.parseInt(battleState.successRate, 10);
  const lines = [];

  if (battleState.dbStatus === "stopped") {
    lines.push("The database is offline.");
  } else if (battleState.dbStatus === "degraded") {
    lines.push("The database is unstable and responding slowly.");
  } else {
    lines.push("The database is still responding.");
  }

  if (battleState.appStatus === "degraded") {
    lines.push("The app is still up, but checkout is only partially working.");
  } else {
    lines.push("The app is still running and serving traffic.");
  }

  if (Number.parseInt(battleState.errorCount, 10) === 0) {
    lines.push("No checkout failures have been observed yet.");
  } else if (successRateValue === 0) {
    lines.push("All tested checkout requests are currently failing.");
  } else if (!Number.isNaN(successRateValue)) {
    lines.push(`${100 - successRateValue}% of tested requests are now failing.`);
  } else {
    lines.push(`${battleState.errorCount} checkout requests have failed so far.`);
  }

  return lines;
}

function renderBattleSummary() {
  const battleSummaryList = document.getElementById("battleSummaryList");
  const lines = buildBattleSummaryLines();

  battleSummaryList.innerHTML = lines
    .map((line) => `<li>${line}</li>`)
    .join("");
}

function renderEnvironmentInfo() {
  const serviceCount = document.querySelectorAll(".service-card").length;
  document.getElementById("environmentContainerCount").textContent = `${serviceCount} monitored`;
}

function getAppServiceCopy(status) {
  if (status === "stopped") {
    return "Checkout requests are failing because the app cannot reach the database.";
  }

  if (status === "degraded") {
    return "The app is still running, but checkout is unstable and some requests are failing.";
  }

  return "The app is running normally and still serving requests.";
}

function getDbServiceCopy(status) {
  if (status === "stopped") {
    return "The database is offline, so checkout cannot read or write order data.";
  }

  if (status === "degraded") {
    return "The database is responding slowly, which is delaying checkout.";
  }

  return "The database is online and responding normally.";
}

function setStatusAppearance(servicePrefix, status) {
  const dot = document.getElementById(`${servicePrefix}StatusDot`);
  const text = document.getElementById(`${servicePrefix}StatusText`);
  const pill = document.getElementById(`${servicePrefix}StatusPill`);
  const card = document.getElementById(`${servicePrefix}ServiceCard`);

  dot.className = "status-dot";
  pill.className = "status-pill";
  card.className = "service-card";

  if (status === "stopped") {
    dot.classList.add("status-stopped");
    pill.classList.add("status-pill-stopped");
    card.classList.add("service-card-stopped");
  } else if (status === "degraded") {
    dot.classList.add("status-degraded");
    pill.classList.add("status-pill-degraded");
    card.classList.add("service-card-degraded");
  } else {
    dot.classList.add("status-running");
    pill.classList.add("status-pill-running");
    card.classList.add("service-card-running");
  }

  text.textContent = formatServiceStatusLabel(status);
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

function renderMcpActivity(items) {
  const mcpActivityList = document.getElementById("mcpActivityList");
  const normalizedItems = [...items]
    .reverse()
    .map((item) => item.replace(/^MCP /, ""));
  const fallbackItems = currentPlan?.drillType === "db_down"
    ? [
        "Initializing failure simulation",
        "Preparing container control",
        "Monitoring database and app state"
      ]
    : [
        "Monitoring system behavior",
        "Collecting live metrics",
        "Analyzing service health"
      ];
  const renderedItems = normalizedItems.length ? normalizedItems : fallbackItems;

  mcpActivityList.innerHTML = renderedItems
    .map((item) => `<li class="mcp-activity-item">${item}</li>`)
    .join("");
}

function renderBattleState() {
  setStatusAppearance("app", battleState.appStatus);
  setStatusAppearance("db", battleState.dbStatus);

  document.getElementById("successRate").textContent = battleState.successRate;
  document.getElementById("errorCount").textContent = battleState.errorCount;
  document.getElementById("p95Latency").textContent = battleState.p95Latency;
  document.getElementById("firstFailure").textContent = battleState.firstFailure;
  document.getElementById("appServiceCopy").textContent = getAppServiceCopy(battleState.appStatus);
  document.getElementById("dbServiceCopy").textContent = getDbServiceCopy(battleState.dbStatus);
  document.getElementById("progressBar").style.width = `${battleState.progressPercent}%`;
  document.getElementById("drillStatusText").textContent = battleState.statusText;
  document.getElementById("errorCountCard").classList.toggle(
    "metric-card-critical",
    Number.parseInt(battleState.errorCount, 10) > 0
  );
  document.getElementById("successRateCard").classList.toggle(
    "metric-card-warning",
    battleState.successRate !== "100%"
  );

  renderTimeline(battleState.timeline);
  renderMcpActivity(battleState.mcpActivity);
  renderBattleSummary();
  renderEnvironmentInfo();
}

function applyDrillStatus(statusData) {
  const timelineEvents = statusData.timeline.map((entry) => {
    const [time, ...rest] = entry.split(" - ");
    return {
      time,
      text: humanizeTimelineText(rest.join(" - "))
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
  battleState.mcpActivity = statusData.mcp_activity || [];
  battleState.timeline = timelineEvents;
  battleState.progressPercent = statusData.status === "complete"
    ? 100
    : battleState.progressPercent;
  battleState.statusText = statusData.status === "complete"
    ? "Simulation complete. Review what changed, then view the verdict."
    : "Simulation in progress...";

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

async function fetchActionPlan() {
  console.log("[WARROOM] action plan fetch start");

  const response = await fetch("http://127.0.0.1:8000/drill/action-plan");

  if (!response.ok) {
    throw new Error(`Action plan request failed with status ${response.status}`);
  }

  const data = await response.json();
  console.log("[WARROOM] action plan fetch success", data);
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

function setResetButtonState(isBusy) {
  const resetButton = document.getElementById("resetButton");
  if (!resetButton) {
    return;
  }

  resetButton.disabled = isBusy;
  resetButton.textContent = isBusy ? "Resetting..." : "Reset & Try Another";
}

function buildVerdictState(evidenceData) {
  const timelineLines = battleState.timeline.map((event) => `${event.time} - ${event.text}`);
  const firstFailure = evidenceData.first_failure_time === null
    ? "--"
    : `00:${String(evidenceData.first_failure_time).padStart(2, "0")}`;

  return {
    result: "FAIL",
    summary: evidenceData.summary || "The system did not handle the dependency failure safely.",
    successRate: `${evidenceData.success_rate}%`,
    p95Latency: `${evidenceData.p95_latency}ms`,
    errorCount: String(evidenceData.error_count),
    firstFailure,
    likelyCause: evidenceData.likely_cause,
    suggestedFix: evidenceData.suggested_fix,
    nextActions: buildNextActions(evidenceData),
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

function buildNextActions(evidenceData) {
  const reasoningText = [
    evidenceData.likely_cause || "",
    evidenceData.suggested_fix || "",
    evidenceData.summary || ""
  ].join(" ").toLowerCase();

  if ((currentPlan && currentPlan.drillType === "db_down") || reasoningText.includes("database")) {
    return [
      "Restore database availability and confirm checkout can complete again.",
      "Add fallback behavior so checkout fails gracefully when the database is unreachable.",
      "Add retry or circuit-breaker protection around database calls in the checkout path."
    ];
  }

  return [
    "Restore the failing dependency or traffic path and confirm the app recovers.",
    "Add graceful fallback behavior for the impacted user flow.",
    "Add protection such as retries, timeouts, or circuit breakers around the weak dependency."
  ];
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
  document.getElementById("nextActionsList").innerHTML = verdictState.nextActions
    .map((action) => `<li>${action}</li>`)
    .join("");
  document.getElementById("evidenceTimeline").textContent = verdictState.evidence.timelineLines.join("\n");
  document.getElementById("evidenceLogs").textContent = verdictState.evidence.logLines.join("\n");
  document.getElementById("evidenceMetrics").textContent = verdictState.evidence.metricsSnapshot.join("\n");
  resetEvidencePanel();
}

function populateActionPlanScreen() {
  if (!actionPlanState) {
    return;
  }

  document.getElementById("actionPlanDoNow").innerHTML = actionPlanState.do_now
    .map((item) => `<li>${item}</li>`)
    .join("");
  document.getElementById("actionPlanFixInCode").innerHTML = actionPlanState.fix_in_code
    .map((item) => `<li>${item}</li>`)
    .join("");
  document.getElementById("actionPlanImproveLater").innerHTML = actionPlanState.improve_later
    .map((item) => `<li>${item}</li>`)
    .join("");
}

async function showVerdictScreen() {
  if (verdictTransitionInFlight) {
    return;
  }

  verdictTransitionInFlight = true;

  try {
    console.log("[WARROOM] verdict transition triggered");
    const evidenceData = await fetchDrillEvidence();
    verdictState = buildVerdictState(evidenceData);
    populateVerdictScreen();
    showScreen("screen4");
    console.log("[WARROOM] transition to verdict screen");
  } catch (error) {
    verdictTransitionInFlight = false;
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
      if (!battleCompleted) {
        battleCompleted = true;
        battleState.progressPercent = 100;
        renderBattleState();
        setViewVerdictButtonVisible(true);
        console.log("[WARROOM] battle screen completed, waiting for manual verdict");
      }
    }
  } catch (error) {
    console.error("[WARROOM] drill status polling failed", error);
    stopDrillStatusPolling();
    alert("Could not load drill status. Please make sure the backend is running.");
  }
}

function parseDurationSeconds(durationText) {
  const parsed = Number.parseInt(durationText, 10);
  return Number.isNaN(parsed) ? 60 : parsed;
}

function startProgressAnimation() {
  const durationSeconds = parseDurationSeconds(currentPlan?.duration || "60 seconds");
  battleStartedAt = Date.now();
  console.log("[WARROOM] battle screen started");
  battleState.progressPercent = 0;
  renderBattleState();

  progressIntervalId = window.setInterval(() => {
    if (!battleStartedAt) {
      return;
    }

    if (battleCompleted || battleState.progressPercent >= 100) {
      battleState.progressPercent = 100;
      document.getElementById("progressBar").style.width = "100%";
      clearInterval(progressIntervalId);
      progressIntervalId = null;
      return;
    }

    const elapsedMs = Date.now() - battleStartedAt;
    const percent = Math.min((elapsedMs / (durationSeconds * 1000)) * 100, 99);
    battleState.progressPercent = percent;
    document.getElementById("progressBar").style.width = `${percent}%`;
  }, 100);
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

  startProgressAnimation();
  pollDrillStatus();
  drillStatusPollingId = window.setInterval(pollDrillStatus, 1000);
}

function viewVerdict() {
  if (!battleCompleted || verdictTransitionInFlight) {
    return;
  }

  console.log("[WARROOM] view verdict clicked");
  void showVerdictScreen();
}

function backToSimulation() {
  console.log("[WARROOM] back to simulation clicked");
  showScreen("screen3");
  renderBattleState();
}

async function showActionPlan() {
  try {
    const data = await fetchActionPlan();
    actionPlanState = data;
    populateActionPlanScreen();
    showScreen("screen5");
  } catch (error) {
    console.error("[WARROOM] action plan fetch failure", error);
    alert("Could not load action plan. Please make sure the backend is running.");
  }
}

function backToVerdict() {
  showScreen("screen4");
}

async function approveRun() {
  if (!currentPlan) {
    alert("Could not start drill. Please make sure the backend is running.");
    return;
  }

  const selectedDuration = document.getElementById("durationSelect").value;
  const selectedIntensity = document.getElementById("intensitySelect").value;

  currentPlan.duration = selectedDuration;
  currentPlan.intensity = selectedIntensity;

  try {
    console.log("[WARROOM] approved drill configuration", {
      drillType: currentPlan.drillType,
      duration: selectedDuration,
      intensity: selectedIntensity
    });

    const data = await startDrillRequest(
      currentPlan.drillType,
      selectedDuration,
      selectedIntensity
    );
    currentDrillId = data.drill_id;

    console.log("[WARROOM] drill started", {
      drillId: currentDrillId,
      drillType: currentPlan.drillType,
      duration: selectedDuration,
      intensity: selectedIntensity
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
  if (resetInProgress) {
    return;
  }

  console.log("[WARROOM] reset button clicked");
  resetInProgress = true;
  setResetButtonState(true);

  try {
    await resetDrillRequest();
    currentDrillId = null;
    currentPlan = null;
    resetBattleState();
    showScreen("screen1");
    console.log("[WARROOM] UI returned to screen 1");
  } catch (error) {
    console.error("[WARROOM] reset failure", error);
    alert("Could not reset drill. Please make sure the backend is running.");
  } finally {
    resetInProgress = false;
    setResetButtonState(false);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("durationSelect").addEventListener("change", updateApprovalHelper);
  document.getElementById("intensitySelect").addEventListener("change", updateApprovalHelper);
  setResetButtonState(false);
  resetBattleState();
  showScreen("screen1");
  console.log("[WARROOM] initialized");
});
