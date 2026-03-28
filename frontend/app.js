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
  },
  credential_exposure: {
    label: "Credential Exposure Risk",
    targetService: "auth and secret handling",
    duration: "45 seconds",
    impact: "Account takeover risk rises if credentials are exposed or reused"
  },
  pii_exposure: {
    label: "PII Exposure Risk",
    targetService: "client data paths",
    duration: "45 seconds",
    impact: "Sensitive client data may leak through weak response controls"
  },
  dependency_api_failure: {
    label: "Dependency API Failure",
    targetService: "third-party integrations",
    duration: "45 seconds",
    impact: "Critical app flows can degrade when upstream APIs fail"
  },
  ai_risk_suite: {
    label: "AI Decide (Top Risk Suite)",
    targetService: "full application risk profile",
    duration: "90 seconds",
    impact: "Runs a short suite across top vulnerabilities and summarizes highest risks"
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
let aiInterpretationLines = [];
let aiInterpretationRequestInFlight = false;
let interpretationRefreshCounter = 0;
let maxUnlockedStep = 1;

const STEP_SCREEN_MAP = {
  1: "screen1",
  2: "screen2",
  3: "screen3",
  4: "screen4"
};

function setMaxUnlockedStep(step) {
  maxUnlockedStep = Math.max(maxUnlockedStep, Math.min(step, 4));
}

function getStepFromScreenId(screenId) {
  const entry = Object.entries(STEP_SCREEN_MAP).find(([, id]) => id === screenId);
  return entry ? Number(entry[0]) : 1;
}

function updateStepIndicator(activeStep) {
  for (let step = 1; step <= 4; step += 1) {
    const button = document.getElementById(`stepBtn${step}`);
    if (!button) {
      continue;
    }

    const isUnlocked = step <= maxUnlockedStep;
    const isActive = step === activeStep;

    button.classList.toggle("active", isActive);
    button.classList.toggle("completed", !isActive && step < activeStep && step <= maxUnlockedStep);
    button.classList.toggle("locked", !isUnlocked);
    button.disabled = !isUnlocked;
  }
}

function goToStep(step) {
  if (step > maxUnlockedStep) {
    return;
  }

  if (step === 1) {
    showScreen("screen1");
    return;
  }

  if (step === 2) {
    if (!currentPlan) {
      return;
    }
    showScreen("screen2");
    return;
  }

  if (step === 3) {
    if (!currentDrillId && !battleCompleted) {
      return;
    }
    showScreen("screen3");
    renderBattleState();
    return;
  }

  if (step === 4) {
    if (!battleCompleted && !verdictState) {
      return;
    }
    if (!verdictState) {
      void showVerdictScreen();
      return;
    }
    showScreen("screen4");
  }
}

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

function runAIDecide() {
  const plan = {
    drillType: "ai_risk_suite",
    label: DRILL_CONFIG.ai_risk_suite.label,
    targetService: DRILL_CONFIG.ai_risk_suite.targetService,
    duration: DRILL_CONFIG.ai_risk_suite.duration,
    impact: DRILL_CONFIG.ai_risk_suite.impact
  };

  currentPlan = plan;
  setMaxUnlockedStep(2);
  populateApprovalScreen(plan);
  showScreen("screen2");
  console.log("[WARROOM] AI decide mode selected");
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

  const step = getStepFromScreenId(screenToShowId);
  updateStepIndicator(step);

  console.log("[WARROOM] screen transition:", screenToShowId);
}

function updateApprovalHelper() {
  const duration = document.getElementById("durationSelect").value;
  const intensity = document.getElementById("intensitySelect").value;
  document.getElementById("approvalHelper").textContent =
    `This drill will run for ${duration} at ${intensity} intensity.`;
  updateThreatLevelIndicator(intensity);
  syncApprovalToggles();
  pulseImpactPanel();
}

function getDrillAccentClass(drillType) {
  if (drillType === "db_down") {
    return "mission-header-danger";
  }

  return "mission-header-warning";
}

function updateMissionHeader(plan) {
  const missionHeader = document.getElementById("missionHeader");
  const missionIcon = document.getElementById("missionIcon");

  missionHeader.classList.remove("mission-header-danger", "mission-header-warning");
  missionIcon.classList.remove("mission-icon-danger", "mission-icon-warning");

  const accentClass = getDrillAccentClass(plan.drillType);
  missionHeader.classList.add(accentClass);
  missionIcon.classList.add(accentClass.replace("mission-header", "mission-icon"));
}

function updateThreatLevelIndicator(intensity) {
  const bars = Array.from(document.querySelectorAll("#threatBars .threat-bar"));
  const threatText = document.getElementById("threatText");
  const intensityLevel = intensity === "High" ? 3 : intensity === "Medium" ? 2 : 1;

  bars.forEach((bar, index) => {
    bar.classList.remove("is-active", "is-danger", "is-warning");
    if (index < intensityLevel) {
      bar.classList.add("is-active");
      bar.classList.add(intensity === "High" ? "is-danger" : "is-warning");
    }
  });

  threatText.textContent = `${intensity} intensity`;
}

function syncApprovalToggles() {
  const duration = document.getElementById("durationSelect").value;
  const intensity = document.getElementById("intensitySelect").value;

  document.querySelectorAll("[data-duration]").forEach((button) => {
    button.classList.toggle("selected", button.dataset.duration === duration);
  });

  document.querySelectorAll("[data-intensity]").forEach((button) => {
    const isSelected = button.dataset.intensity === intensity;
    button.classList.remove("selected", "selected-success", "selected-warning", "selected-danger");
    if (!isSelected) {
      return;
    }

    button.classList.add("selected");
    if (intensity === "Low") {
      button.classList.add("selected-success");
    } else if (intensity === "High") {
      button.classList.add("selected-danger");
    } else {
      button.classList.add("selected-warning");
    }
  });
}

function pulseImpactPanel() {
  const impactPanel = document.getElementById("impactPanel");
  impactPanel.classList.remove("impact-refresh");
  void impactPanel.offsetWidth;
  impactPanel.classList.add("impact-refresh");
}

function populateApprovalScreen(plan) {
  document.getElementById("drillType").textContent = plan.label;
  document.getElementById("targetService").textContent = plan.targetService;
  document.getElementById("durationSelect").value = plan.duration;
  document.getElementById("intensitySelect").value = "Medium";
  document.getElementById("impact").textContent = plan.impact;
  updateMissionHeader(plan);
  updateApprovalHelper();
}

function setDurationOption(duration) {
  document.getElementById("durationSelect").value = duration;
  updateApprovalHelper();
}

function setIntensityOption(intensity) {
  document.getElementById("intensitySelect").value = intensity;
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
    setMaxUnlockedStep(2);

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
  aiInterpretationLines = [];
  aiInterpretationRequestInFlight = false;
  interpretationRefreshCounter = 0;
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

function generateLiveNarration() {
  const drillType = currentPlan?.drillType || "db_down";
  const successRateValue = Number.parseInt(battleState.successRate, 10);
  const errorCountValue = Number.parseInt(battleState.errorCount, 10);
  const latencyValue = Number.parseInt(battleState.p95Latency, 10);
  const hasMcpActivity = battleState.mcpActivity.length > 0;
  const hasFirstFailure = battleState.firstFailure !== "--";
  const lines = [];

  if (drillType === "db_down") {
    if (battleState.dbStatus === "stopped") {
      lines.push("WARROOM intentionally took the database offline through MCP.");
    } else if (hasMcpActivity) {
      lines.push("WARROOM is using MCP to control the database failure simulation and watch the blast radius.");
    } else {
      lines.push("WARROOM is preparing a database failure simulation and watching the checkout path closely.");
    }

    if (errorCountValue > 0 || hasFirstFailure) {
      lines.push("Checkout is now failing because the app cannot reach its database dependency.");
    } else if (battleState.appStatus === "degraded") {
      lines.push("The app is still running, but the checkout path is weakening as database access becomes unstable.");
    } else {
      lines.push("The app is still up, but WARROOM is watching for user-facing impact on checkout.");
    }

    if (!Number.isNaN(successRateValue) && successRateValue === 0) {
      lines.push("Users would currently be unable to complete purchases.");
    } else if (!Number.isNaN(successRateValue) && successRateValue < 100) {
      lines.push("The blast radius has reached the purchase flow and some users would now see failed checkout attempts.");
    } else {
      lines.push("No checkout failures are visible yet, but WARROOM is still collecting live evidence.");
    }

    if (battleCompleted) {
      lines.push("WARROOM has collected enough evidence to show that this database outage directly undermines checkout reliability.");
    } else if (battleState.dbStatus === "stopped") {
      lines.push("Failure impact is spreading from infrastructure to the user-facing checkout flow.");
    } else {
      lines.push("This drill is testing how quickly a database dependency failure becomes visible to users.");
    }
  } else if (drillType === "latency_spike") {
    if (hasMcpActivity) {
      lines.push("WARROOM injected latency into the database path through MCP.");
    } else {
      lines.push("WARROOM is simulating a slower database path and observing the impact on checkout.");
    }

    if (!Number.isNaN(latencyValue) && latencyValue >= 800) {
      lines.push("The database is still online, but checkout is responding much more slowly.");
    } else {
      lines.push("The system is still serving traffic, but response pressure is building along the checkout path.");
    }

    if (errorCountValue > 0 || hasFirstFailure) {
      lines.push("Users may still complete checkout, but the degraded dependency is now creating visible errors.");
    } else {
      lines.push("Users may still complete checkout, but the experience is degraded and failures could follow.");
    }

    if (battleCompleted) {
      lines.push("WARROOM has confirmed that dependency slowdown can erode checkout reliability before a full outage occurs.");
    } else {
      lines.push("If latency continues rising, slow responses may turn into timeouts or failed purchases.");
    }
  } else if (drillType === "credential_exposure") {
    lines.push("WARROOM is simulating credential exposure patterns across authentication and secret handling.");

    if (errorCountValue > 0 || hasFirstFailure) {
      lines.push("The current posture suggests account misuse risk if leaked credentials are replayed.");
    } else {
      lines.push("No immediate customer-visible failure yet, but identity safety risk is elevated.");
    }

    lines.push("This test focuses on reducing takeover risk before attackers can exploit weak secret controls.");

    if (battleCompleted) {
      lines.push("WARROOM has captured enough evidence to prioritize credential protection fixes.");
    } else {
      lines.push("WARROOM is still collecting evidence from authentication behavior and simulated abuse signals.");
    }
  } else if (drillType === "pii_exposure") {
    lines.push("WARROOM is simulating data exposure pressure on client-data paths.");

    if (errorCountValue > 0 || hasFirstFailure) {
      lines.push("Weak data boundaries are now visible and could expose sensitive client information.");
    } else {
      lines.push("Risk remains latent, but data boundary weaknesses can become user-impacting quickly.");
    }

    lines.push("This drill validates whether data minimization and access controls are strong enough.");

    if (battleCompleted) {
      lines.push("WARROOM has identified concrete points where privacy protections should be tightened.");
    } else {
      lines.push("WARROOM is still mapping where sensitive fields could leak through responses.");
    }
  } else if (drillType === "dependency_api_failure") {
    lines.push("WARROOM is simulating a third-party API outage in a critical app path.");

    if (errorCountValue > 0 || hasFirstFailure) {
      lines.push("User flow reliability is dropping because fallback behavior is not absorbing upstream failure.");
    } else {
      lines.push("The integration is under stress and fallback quality is being validated.");
    }

    lines.push("This test shows how quickly external dependency failures can reach users.");

    if (battleCompleted) {
      lines.push("WARROOM has gathered evidence to prioritize timeout, retry, and fallback improvements.");
    } else {
      lines.push("WARROOM is still observing dependency error propagation through the app.");
    }
  } else if (drillType === "ai_risk_suite") {
    lines.push("WARROOM AI selected a short top-risk suite for full-app vulnerability coverage.");

    if (errorCountValue > 0 || hasFirstFailure) {
      lines.push("The suite is already surfacing real user-impact patterns across multiple risk categories.");
    } else {
      lines.push("Suite execution is in progress across credential, data, and dependency risk scenarios.");
    }

    lines.push("This mode is designed for teams who are unsure what to test first.");

    if (battleCompleted) {
      lines.push("WARROOM now has a ranked risk summary and prioritized actions for the app.");
    } else {
      lines.push("WARROOM is collecting cross-scenario evidence to produce a prioritized final verdict.");
    }
  } else {
    lines.push("WARROOM is pushing sustained traffic into the checkout path and watching how the system absorbs load.");

    if (errorCountValue > 0 || hasFirstFailure) {
      lines.push("The app is still responding, but pressure is turning into visible checkout failures.");
    } else if (!Number.isNaN(latencyValue) && latencyValue > 300) {
      lines.push("The app is still responding, but request pressure is pushing response delay upward.");
    } else {
      lines.push("The system is still serving requests, but WARROOM is monitoring for rising latency and saturation.");
    }

    if (!Number.isNaN(successRateValue) && successRateValue < 100) {
      lines.push("Reliability is starting to fall as sustained traffic keeps stressing the checkout flow.");
    } else {
      lines.push("Users may still succeed for now, but the safety margin is shrinking under load.");
    }

    if (battleCompleted) {
      lines.push("WARROOM has gathered enough evidence to show how traffic pressure changes the risk profile of checkout.");
    } else {
      lines.push("If traffic continues rising, latency and failure risk will keep increasing.");
    }
  }

  return lines.slice(0, 4);
}

async function fetchLiveInterpretation() {
  const response = await fetch("http://127.0.0.1:8000/drill/live-interpretation");

  if (!response.ok) {
    throw new Error(`Live interpretation request failed with status ${response.status}`);
  }

  const data = await response.json();
  return Array.isArray(data.lines) ? data.lines.filter((line) => typeof line === "string" && line.trim()).slice(0, 4) : [];
}

function maybeRefreshAiInterpretation(force = false) {
  if (aiInterpretationRequestInFlight) {
    return;
  }

  if (!currentDrillId) {
    return;
  }

  if (!force && interpretationRefreshCounter % 2 !== 0) {
    return;
  }

  aiInterpretationRequestInFlight = true;

  void fetchLiveInterpretation()
    .then((lines) => {
      if (lines.length > 0) {
        aiInterpretationLines = lines;
        renderLiveInterpretation();
      }
    })
    .catch(() => {
      // Deterministic narration remains the primary safe path.
    })
    .finally(() => {
      aiInterpretationRequestInFlight = false;
    });
}

function renderLiveInterpretation() {
  const interpretationList = document.getElementById("liveInterpretationList");
  if (!interpretationList) {
    return;
  }
  const lines = aiInterpretationLines.length > 0 ? aiInterpretationLines : generateLiveNarration();

  interpretationList.innerHTML = lines
    .map((line) => `<li class="interpretation-item">${line}</li>`)
    .join("");
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
  renderLiveInterpretation();
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
  interpretationRefreshCounter += 1;

  renderBattleState();
  maybeRefreshAiInterpretation(statusData.status === "complete");
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

async function fetchRemediationPrompt(drillType) {
  const response = await fetch(`http://127.0.0.1:8000/remediation/prompt?drill_type=${encodeURIComponent(drillType)}`);

  if (!response.ok) {
    throw new Error(`Remediation prompt request failed with status ${response.status}`);
  }

  return response.json();
}

async function applyRemediationPrompt(promptText, drillType) {
  const response = await fetch("http://127.0.0.1:8000/remediation/apply", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      prompt: promptText,
      drill_type: drillType
    })
  });

  if (!response.ok) {
    throw new Error(`Remediation apply request failed with status ${response.status}`);
  }

  return response.json();
}

async function verifyRemediation(drillType) {
  const response = await fetch("http://127.0.0.1:8000/remediation/verify", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      drill_type: drillType
    })
  });

  if (!response.ok) {
    throw new Error(`Remediation verify request failed with status ${response.status}`);
  }

  return response.json();
}

function setRemediationStatusText(message, isResolved = false) {
  const status = document.getElementById("remediationStatusText");
  if (!status) {
    return;
  }

  status.textContent = message;
  status.classList.toggle("remediation-status-pass", isResolved);
}

async function generateFixPrompt() {
  if (!currentPlan) {
    alert("No drill context available. Run a drill first.");
    return;
  }

  try {
    const data = await fetchRemediationPrompt(currentPlan.drillType);
    const textarea = document.getElementById("remediationPromptInput");
    textarea.value = data.prompt || "";
    setRemediationStatusText("Fix prompt generated. Review and apply when ready.");
  } catch (error) {
    console.error("[WARROOM] remediation prompt fetch failed", error);
    alert("Could not generate remediation prompt. Please make sure the backend is running.");
  }
}

async function applyFixPrompt() {
  if (!currentPlan) {
    alert("No drill context available. Run a drill first.");
    return;
  }

  const textarea = document.getElementById("remediationPromptInput");
  const promptText = textarea.value.trim();

  if (!promptText) {
    alert("Paste a remediation prompt first.");
    return;
  }

  try {
    const result = await applyRemediationPrompt(promptText, currentPlan.drillType);
    setRemediationStatusText(`Prompt applied for ${result.drill_type}. Run verification re-test.`);
  } catch (error) {
    console.error("[WARROOM] remediation apply failed", error);
    alert("Could not apply remediation prompt. Please make sure the backend is running.");
  }
}

async function runVerificationRetest() {
  if (!currentPlan) {
    alert("No drill context available. Run a drill first.");
    return;
  }

  try {
    const verification = await verifyRemediation(currentPlan.drillType);

    if (!verification.resolved) {
      setRemediationStatusText("Verification blocked: apply a remediation prompt first.");
      alert("Apply a remediation prompt before running verification re-test.");
      return;
    }

    setRemediationStatusText("Verification passed. Re-running the drill with remediation profile.", true);

    const selectedDuration = document.getElementById("durationSelect").value;
    const selectedIntensity = document.getElementById("intensitySelect").value;

    const data = await startDrillRequest(
      currentPlan.drillType,
      selectedDuration,
      selectedIntensity
    );
    currentDrillId = data.drill_id;
    startDrillStatusPolling();
  } catch (error) {
    console.error("[WARROOM] remediation verification failed", error);
    alert("Could not run verification re-test. Please make sure backend and MCP are running.");
  }
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
    result: evidenceData.resolved ? "PASS" : "FAIL",
    summary: evidenceData.summary || "The system did not handle the dependency failure safely.",
    successRate: `${evidenceData.success_rate}%`,
    p95Latency: `${evidenceData.p95_latency}ms`,
    errorCount: String(evidenceData.error_count),
    firstFailure,
    likelyCause: evidenceData.likely_cause,
    suggestedFix: evidenceData.suggested_fix,
    technicalFindings: buildTechnicalFindings(evidenceData, firstFailure),
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

function buildTechnicalFindings(evidenceData, firstFailure) {
  const reasoningText = [
    evidenceData.likely_cause || "",
    evidenceData.summary || "",
    currentPlan?.drillType || ""
  ].join(" ").toLowerCase();

  if (reasoningText.includes("database") || currentPlan?.drillType === "db_down") {
    return [
      `Impact visible at ${firstFailure}.`,
      `${100 - evidenceData.success_rate}% of sampled requests failed during the drill.`,
      "Checkout depends on a single database path with no graceful fallback."
    ];
  }

  return [
    `${evidenceData.error_count} request failures were observed.`,
    `First user-visible error appeared at ${firstFailure}.`,
    "The critical user flow degraded under dependency stress."
  ];
}

function buildVerdictSignals() {
  if (!verdictState) {
    return null;
  }

  const successRateValue = Number.parseInt(verdictState.successRate, 10);
  const failureRate = Number.isNaN(successRateValue)
    ? "high"
    : `${Math.max(0, 100 - successRateValue)}%`;

  return {
    risk: {
      headline: `${verdictState.result} resilience check`,
      copy: `Checkout reliability is below safe threshold in this drill scenario.`
    },
    impact: {
      headline: `${failureRate} sampled requests failed`,
      copy: `First visible error reached users at ${verdictState.firstFailure}.`
    },
    focus: {
      headline: "Protect the checkout dependency path",
      copy: "Implement graceful fallback, retries, and circuit-breaker safety controls."
    }
  };
}

function buildPriorityActions() {
  const fallback = [
    "Stabilize the failing dependency path.",
    "Add graceful fallback behavior for checkout.",
    "Add retry and circuit-breaker protection."
  ];

  if (!actionPlanState) {
    return fallback;
  }

  const raw = [
    ...(actionPlanState.do_now || []),
    ...(actionPlanState.fix_in_code || []),
    ...(actionPlanState.improve_later || [])
  ]
    .map((item) => (typeof item === "string" ? item.trim() : ""))
    .filter(Boolean);

  const deduped = [];
  const seen = new Set();
  for (const item of raw) {
    const key = item.toLowerCase();
    if (!seen.has(key)) {
      seen.add(key);
      deduped.push(item);
    }
  }

  return deduped.slice(0, 3).length ? deduped.slice(0, 3) : fallback;
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
  document.getElementById("nextActionsList").innerHTML = verdictState.technicalFindings
    .map((finding) => `<li>${finding}</li>`)
    .join("");

  const signals = buildVerdictSignals();
  if (signals) {
    document.getElementById("signalRiskHeadline").textContent = signals.risk.headline;
    document.getElementById("signalRiskCopy").textContent = signals.risk.copy;
    document.getElementById("signalImpactHeadline").textContent = signals.impact.headline;
    document.getElementById("signalImpactCopy").textContent = signals.impact.copy;
    document.getElementById("signalFocusHeadline").textContent = signals.focus.headline;
    document.getElementById("signalFocusCopy").textContent = signals.focus.copy;
  }

  const priorityActions = buildPriorityActions();
  document.getElementById("priorityActionsList").innerHTML = priorityActions
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
    const [evidenceData, actionPlanData] = await Promise.all([
      fetchDrillEvidence(),
      fetchActionPlan()
    ]);
    verdictState = buildVerdictState(evidenceData);
    actionPlanState = actionPlanData;
    populateVerdictScreen();
    populateActionPlanScreen();
    setMaxUnlockedStep(4);
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
  setMaxUnlockedStep(3);
  showScreen("screen3");
  renderBattleState();

  console.log("[WARROOM] polling started", {
    drillId: currentDrillId,
    drillType: currentPlan ? currentPlan.drillType : "db_down"
  });

  startProgressAnimation();
  pollDrillStatus();
  maybeRefreshAiInterpretation(true);
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
    const approveButton = document.getElementById("approveButton");
    approveButton.classList.add("approval-submit-pulse");
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
  currentPlan = null;
  maxUnlockedStep = 1;
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
    maxUnlockedStep = 1;
    resetBattleState();
    const remediationPromptInput = document.getElementById("remediationPromptInput");
    if (remediationPromptInput) {
      remediationPromptInput.value = "";
    }
    setRemediationStatusText("No remediation prompt applied yet.");
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
  document.querySelectorAll("[data-duration]").forEach((button) => {
    button.addEventListener("click", () => setDurationOption(button.dataset.duration));
  });
  document.querySelectorAll("[data-intensity]").forEach((button) => {
    button.addEventListener("click", () => setIntensityOption(button.dataset.intensity));
  });
  setResetButtonState(false);
  setRemediationStatusText("No remediation prompt applied yet.");
  updateStepIndicator(1);
  resetBattleState();
  showScreen("screen1");
  console.log("[WARROOM] initialized");
});
