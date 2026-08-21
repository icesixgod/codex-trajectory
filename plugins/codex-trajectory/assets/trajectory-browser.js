(() => {
  "use strict";

  const viewer = document.getElementById("viewer");
  const params = new URLSearchParams(location.search);
  const initialSessionId = params.get("sessionId");
  let stopSessionId = initialSessionId;
  const language = params.get("lang") || navigator.language || "en";
  const apiUrl = new URL("api/tool", location.href);
  const stopApiUrl = new URL("api/stop", location.href);
  const taskStateApiUrl = new URL("api/task-state", location.href);
  const themeApiUrl = new URL("api/theme", location.href);
  const stopBridgeEnabled = "__CODEX_TRAJECTORY_STOP_BRIDGE__";
  const TASK_STATE_INTERVAL_MS = 750;
  let currentTheme = null;
  let themeSignature = "";
  let taskStateBusy = false;
  let projectedTurnId = null;
  let rejectedProjectedTurnId = null;

  const captureProjectedTurn = result => {
    const trajectory = result?.structuredContent;
    const sessionId = trajectory?.session?.id;
    if (typeof sessionId !== "string") return;
    if (stopSessionId === null) stopSessionId = sessionId;
    if (sessionId !== stopSessionId || !Array.isArray(trajectory.turns)) return;
    const activeTurn = [...trajectory.turns]
      .reverse()
      .find(turn => turn?.status === "running" || turn?.status === "inProgress");
    const activeTurnId = typeof activeTurn?.id === "string" ? activeTurn.id : null;
    if (activeTurnId && activeTurnId !== rejectedProjectedTurnId) {
      projectedTurnId = activeTurnId;
      rejectedProjectedTurnId = null;
    } else {
      projectedTurnId = null;
    }
  };

  const callTool = async (name, argumentsValue = {}) => {
    const response = await fetch(apiUrl, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, arguments: argumentsValue }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const result = await response.json();
    captureProjectedTurn(result);
    return result;
  };

  const requestStop = async parameters => {
    const requestedSessionId = parameters?.sessionId;
    if (
      typeof requestedSessionId !== "string"
      || !requestedSessionId
      || (stopSessionId !== null && requestedSessionId !== stopSessionId)
    ) {
      return {
        sent: false,
        error: "The displayed trajectory does not match the Codex task that opened this page.",
      };
    }
    const response = await fetch(stopApiUrl, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId: requestedSessionId,
        turnId: parameters?.turnId,
        source: parameters?.source,
        threshold: parameters?.threshold,
        language: language.toLowerCase().startsWith("zh") ? "zh" : "en",
      }),
    });
    const result = await response.json().catch(() => null);
    if (!response.ok) throw new Error(result?.error || `HTTP ${response.status}`);
    if (result?.stale === true && typeof parameters?.turnId === "string") {
      rejectedProjectedTurnId = parameters.turnId;
      projectedTurnId = null;
      void syncTaskState();
    }
    return result;
  };

  const reply = (id, result) => {
    viewer.contentWindow?.postMessage({ jsonrpc: "2.0", id, result }, "*");
  };

  const notify = structuredContent => {
    viewer.contentWindow?.postMessage({
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params: { structuredContent },
    }, "*");
  };

  const notifyTheme = theme => {
    viewer.contentWindow?.postMessage({
      jsonrpc: "2.0",
      method: "trajectory/theme",
      params: theme,
    }, "*");
  };

  const notifyTaskState = state => {
    viewer.contentWindow?.postMessage({
      jsonrpc: "2.0",
      method: "trajectory/task-state",
      params: state,
    }, "*");
  };

  const syncTaskState = async () => {
    if (!stopBridgeEnabled || !stopSessionId || taskStateBusy) return;
    taskStateBusy = true;
    let bootstrapAfterStale = false;
    try {
      const url = new URL(taskStateApiUrl);
      url.searchParams.set("sessionId", stopSessionId);
      const candidateTurnId = projectedTurnId;
      if (candidateTurnId) url.searchParams.set("turnId", candidateTurnId);
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) return;
      const state = await response.json();
      if (
        typeof state?.running !== "boolean"
        || (state.running && typeof state.turnId !== "string")
        || (!state.running && state.turnId !== null)
      ) return;
      if (
        candidateTurnId
        && state.running
        && state.turnId === rejectedProjectedTurnId
      ) {
        // This routine poll was already in flight when Stop rejected its
        // candidate. Do not let the late response resurrect that stale turn;
        // follow it immediately with the one history-bearing bootstrap.
        projectedTurnId = null;
        bootstrapAfterStale = true;
        return;
      }
      projectedTurnId = state.running ? state.turnId : null;
      if (!candidateTurnId || state.turnId !== rejectedProjectedTurnId) {
        rejectedProjectedTurnId = null;
      }
      notifyTaskState({ ...state, sessionId: stopSessionId });
    } catch {
      // Retain the last confirmed App Server state during a transient bridge failure.
    } finally {
      taskStateBusy = false;
      if (bootstrapAfterStale) void syncTaskState();
    }
  };

  const syncTheme = async () => {
    try {
      const response = await fetch(themeApiUrl, { cache: "no-store" });
      if (!response.ok) return;
      const theme = await response.json();
      if (!theme || !["light", "dark"].includes(theme.scheme) || !theme.colors) return;
      const signature = JSON.stringify(theme);
      if (signature === themeSignature) return;
      themeSignature = signature;
      currentTheme = theme;
      document.documentElement.dataset.codexTheme = theme.scheme;
      document.documentElement.style.colorScheme = theme.scheme;
      document.documentElement.style.setProperty("--browser-bg", theme.colors.bg);
      notifyTheme(theme);
    } catch {
      // Keep the last valid Codex palette while the host is temporarily unavailable.
    }
  };

  viewer.addEventListener("load", async () => {
    if (currentTheme) notifyTheme(currentTheme);
    try {
      const result = await callTool("get_codex_trajectory", {
        ...(initialSessionId ? { sessionId: initialSessionId } : {}),
        maxRecords: 500,
        includeArchived: true,
        detailLevel: "summary",
      });
      if (result?.structuredContent) {
        notify(result.structuredContent);
        void syncTaskState();
      }
    } catch (error) {
      document.title = `Codex Trajectory · ${String(error?.message || error)}`;
    }
  });

  window.addEventListener("message", async event => {
    if (event.source !== viewer.contentWindow) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.method === "trajectory/request-stop" && stopBridgeEnabled) {
      try {
        reply(message.id, await requestStop(message.params));
      } catch (error) {
        reply(message.id, { sent: false, error: String(error?.message || error) });
      }
      return;
    }
    if (message.method !== "tools/call") return;
    try {
      reply(message.id, await callTool(message.params?.name, message.params?.arguments || {}));
    } catch (error) {
      reply(message.id, {
        isError: true,
        content: [{ type: "text", text: String(error?.message || error) }],
      });
    }
  });

  const viewerParams = new URLSearchParams({ lang: language });
  if (stopBridgeEnabled) viewerParams.set("stopBridge", "1");
  viewer.src = `trajectory.html?${viewerParams}`;
  void syncTheme();
  window.setInterval(syncTheme, 1_000);
  void syncTaskState();
  window.setInterval(() => void syncTaskState(), TASK_STATE_INTERVAL_MS);
})();
