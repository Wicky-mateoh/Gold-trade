// Thin fetch wrapper. All routes are served by backend/app/main.py.
async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text.slice(0, 200)}`);
  }
  return res.json();
}

export const api = {
  status: () => request("/api/status"),
  analysis: () => request("/api/analysis"),
  signal: () => request("/api/signal"),
  candles: (tf, limit = 300) => request(`/api/candles?timeframe=${tf}&limit=${limit}`),
  risk: () => request("/api/risk"),
  performance: () => request("/api/performance"),
  journal: (kind, limit = 50) => request(`/api/journal?limit=${limit}${kind ? `&kind=${kind}` : ""}`),
  learning: () => request("/api/learning"),
  backtest: (timeframe) => request("/api/backtest", { method: "POST", body: JSON.stringify({ timeframe }) }),
  latestBacktest: () => request("/api/backtest/latest"),
  setMode: (mode) => request("/api/mode", { method: "POST", body: JSON.stringify({ mode }) }),
  requestLive: () =>
    request("/api/live/request", {
      method: "POST",
      body: JSON.stringify({ explicit_authorization: true, broker_connected: true, account_risk_limits_set: true, emergency_shutdown_armed: true }),
    }),
  markPaper: () => request("/api/paper/mark", { method: "POST" }),
  shutdown: (reason) => request("/api/emergency/shutdown", { method: "POST", body: JSON.stringify({ reason }) }),
  resetShutdown: (operator, acknowledgement) =>
    request("/api/emergency/reset", { method: "POST", body: JSON.stringify({ operator, acknowledgement }) }),
  ackLossPause: (operator) =>
    request("/api/risk/acknowledge-loss-pause", { method: "POST", body: JSON.stringify({ operator }) }),
};

export function streamUrl() {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/stream`;
}
