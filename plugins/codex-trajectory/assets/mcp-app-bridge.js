/* Native MCP Apps transport, with compatibility for the window.openai host. */
(() => {
  const PROTOCOL_VERSION = "2026-01-26";
  const REQUEST_TIMEOUT_MS = 60_000;
  const LARGE_LOG_TIMEOUT_MS = 600_000;

  window.createTrajectoryHost = ({ onResult, onContext, onError, onTeardown }) => {
    const pending = new Map();
    let nextId = 1;
    let context = {};
    let connection = null;
    let closed = false;
    const legacy = typeof window.openai?.callTool === "function";
    const post = message => window.parent.postMessage({ jsonrpc: "2.0", ...message }, "*");
    const notify = (method, params = {}) => post({ method, params });

    function withTimeout(promise, timeoutMs = REQUEST_TIMEOUT_MS) {
      let timer;
      const deadline = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("Request timed out.")), timeoutMs);
      });
      return Promise.race([Promise.resolve(promise), deadline]).finally(() => clearTimeout(timer));
    }

    function request(method, params, timeoutMs = REQUEST_TIMEOUT_MS) {
      if (closed) return Promise.reject(new Error("The viewer has closed."));
      const id = `trajectory-${nextId++}`;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error("Request timed out."));
        }, timeoutMs);
        pending.set(id, { resolve, reject, timer });
        post({ id, method, params });
      });
    }

    function updateContext(next) {
      if (!next || typeof next !== "object" || Array.isArray(next)) return;
      context = { ...context, ...next };
      onContext(context);
    }

    function teardown() {
      if (closed) return;
      closed = true;
      for (const handler of pending.values()) {
        clearTimeout(handler.timer);
        handler.reject(new Error("The viewer has closed."));
      }
      pending.clear();
      window.removeEventListener("message", receive);
      window.removeEventListener("openai:set_globals", receiveGlobals);
      onTeardown();
    }

    function receive(event) {
      if (event.source !== window.parent || event.data?.jsonrpc !== "2.0") return;
      const message = event.data;
      if (pending.has(message.id)) {
        const handler = pending.get(message.id);
        pending.delete(message.id);
        clearTimeout(handler.timer);
        if (message.error) handler.reject(new Error(message.error.message || "Host request failed."));
        else handler.resolve(message.result);
        return;
      }
      if (message.method === "ui/notifications/tool-result") onResult(message.params);
      else if (message.method === "ui/notifications/host-context-changed") updateContext(message.params);
      else if (message.method === "ui/resource-teardown") {
        if (message.id !== undefined) post({ id: message.id, result: {} });
        teardown();
      }
    }

    function receiveGlobals(event) {
      const globals = event?.detail?.globals || event?.detail || {};
      updateContext(globals);
      if (globals.toolOutput) onResult({ structuredContent: globals.toolOutput });
    }

    window.addEventListener("message", receive);
    window.addEventListener("openai:set_globals", receiveGlobals);
    window.addEventListener("pagehide", teardown, { once: true });

    function connect() {
      if (connection) return connection;
      // Cache the promise before callbacks can reenter through callTool().
      connection = Promise.resolve().then(async () => {
        if (closed) throw new Error("The viewer has closed.");
        if (legacy) {
          updateContext({
            theme: window.openai.theme,
            locale: window.openai.locale,
            displayMode: window.openai.displayMode,
            availableDisplayModes: typeof window.openai.requestDisplayMode === "function"
              ? ["inline", "fullscreen"] : ["inline"],
          });
          if (window.openai.toolOutput) onResult({ structuredContent: window.openai.toolOutput });
          return;
        }
        const result = await request("ui/initialize", {
          protocolVersion: PROTOCOL_VERSION,
          appInfo: { name: "Codex Trajectory", version: "__TRAJECTORY_VERSION__" },
          appCapabilities: {},
        });
        updateContext(result?.hostContext || {});
        notify("ui/notifications/initialized");
      }).catch(error => {
        connection = null;
        if (!closed) onError(error);
        throw error;
      });
      return connection;
    }

    return Object.freeze({
      withTimeout,
      start: () => connect().catch(() => {}),
      async callTool(name, args) {
        await connect();
        const largeRead = ["get_codex_trajectory", "show_codex_trajectory", "get_codex_trajectory_update"].includes(name)
          && Number.isSafeInteger(args?.maxReadBytes) && args.maxReadBytes > 512 * 1024 * 1024;
        const timeoutMs = largeRead ? LARGE_LOG_TIMEOUT_MS : REQUEST_TIMEOUT_MS;
        if (legacy) return withTimeout(window.openai.callTool(name, args), timeoutMs);
        return request("tools/call", { name, arguments: args }, timeoutMs);
      },
      supportsDisplayMode: mode => (context.availableDisplayModes || []).includes(mode),
      async requestDisplayMode(mode) {
        await connect();
        if (legacy) return withTimeout(window.openai.requestDisplayMode({ mode }));
        return request("ui/request-display-mode", { mode });
      },
    });
  };
})();
