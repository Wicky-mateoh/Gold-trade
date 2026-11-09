import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamUrl } from "./api.js";
import { fmt, tone } from "./format.js";
import Card, { Badge } from "./components/Card.jsx";
import PriceChart from "./components/PriceChart.jsx";
import {
  CalendarPanel,
  IndicatorsPanel,
  JournalPanel,
  LiquidityPanel,
  MtfTable,
  NewsPanel,
  NfpCpiPanel,
  PerformancePanel,
  RiskPanel,
  SignalPanel,
} from "./components/Panels.jsx";

const CHART_TFS = ["M5", "M15", "H1", "H4", "D1", "W1", "MN1"];
const REFRESH_MS = 30000;

export default function App() {
  const [status, setStatus] = useState(null);
  const [analysis, setAnalysis] = useState(null);
  const [evaluation, setEvaluation] = useState(null);
  const [candles, setCandles] = useState([]);
  const [chartTf, setChartTf] = useState("H1");
  const [risk, setRisk] = useState(null);
  const [performance, setPerformance] = useState(null);
  const [backtest, setBacktest] = useState(null);
  const [learning, setLearning] = useState(null);
  const [journal, setJournal] = useState([]);
  const [journalKind, setJournalKind] = useState("");
  const [toasts, setToasts] = useState([]);
  const [error, setError] = useState(null);
  const [running, setRunning] = useState(false);
  const [live, setLive] = useState(false);
  const lastDecision = useRef(null);

  const notify = useCallback((text, kind = "info") => {
    const id = Math.random().toString(36).slice(2);
    setToasts((t) => [...t.slice(-3), { id, text, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 7000);
    if (kind !== "info" && "Notification" in window && Notification.permission === "granted") {
      new Notification("GITS · XAU/USD", { body: text });
    }
  }, []);

  const onEvaluation = useCallback(
    (ev) => {
      setEvaluation(ev);
      const decision = ev?.signal?.decision;
      if (lastDecision.current && decision && decision !== lastDecision.current) {
        notify(`Decision changed: ${lastDecision.current.replace("_", " ")} → ${decision.replace("_", " ")}`, decision === "NO_TRADE" ? "info" : "signal");
      }
      lastDecision.current = decision;
    },
    [notify],
  );

  const refresh = useCallback(async () => {
    try {
      const [st, an, ev, rk, pf, lr, bt] = await Promise.all([
        api.status(), api.analysis(), api.signal(), api.risk(), api.performance(), api.learning(), api.latestBacktest(),
      ]);
      setStatus(st); setAnalysis(an); onEvaluation(ev); setRisk(rk); setPerformance(pf); setLearning(lr);
      if (bt?.metrics) setBacktest(bt);
      setError(null);
    } catch (e) {
      setError(String(e.message || e));
    }
  }, [onEvaluation]);

  useEffect(() => { refresh(); const t = setInterval(refresh, REFRESH_MS); return () => clearInterval(t); }, [refresh]);
  useEffect(() => { api.candles(chartTf, 300).then((r) => setCandles(r.candles)).catch(() => {}); }, [chartTf, analysis?.timestamp]);
  useEffect(() => { api.journal(journalKind || null, 60).then(setJournal).catch(() => {}); }, [journalKind, evaluation]);

  // Real-time stream: preview evaluations pushed by the server (never opens positions).
  useEffect(() => {
    let ws;
    let closed = false;
    const connect = () => {
      ws = new WebSocket(streamUrl());
      ws.onopen = () => setLive(true);
      ws.onmessage = (msg) => {
        const data = JSON.parse(msg.data);
        if (data.type === "evaluation") onEvaluation(data.data);
      };
      ws.onclose = () => { setLive(false); if (!closed) setTimeout(connect, 5000); };
    };
    connect();
    return () => { closed = true; ws && ws.close(); };
  }, [onEvaluation]);

  const act = async (fn, ok) => {
    try { const r = await fn(); if (ok) notify(ok(r), "info"); await refresh(); return r; }
    catch (e) { notify(String(e.message || e), "error"); }
  };

  const runBacktest = async () => {
    setRunning(true);
    try {
      const r = await api.backtest();
      setBacktest(r);
      setLearning(await api.learning());
      notify(`Backtest done: ${r.metrics.trades} trades, walk-forward ${r.out_of_sample.passed ? "passed" : "not passed"}`, "info");
    } catch (e) { notify(String(e.message || e), "error"); }
    setRunning(false);
  };

  const shutdown = () => {
    const reason = window.prompt("Emergency shutdown reason:");
    if (reason && reason.length >= 3) act(() => api.shutdown(reason), (r) => `Emergency shutdown active · ${r.flattened} paper positions flattened`);
  };
  const resetShutdown = () => {
    const operator = window.prompt("Operator name:");
    if (!operator) return;
    const ack = window.prompt('Type exactly: "I have reviewed the shutdown cause"');
    act(() => api.resetShutdown(operator, ack || ""), (r) => (r.reset ? "Emergency shutdown cleared" : r.reason));
  };
  const ackPause = () => {
    const operator = window.prompt("Operator name:");
    if (operator) act(() => api.ackLossPause(operator), () => "Loss pause acknowledged");
  };

  const dir = analysis?.direction;
  const signal = evaluation?.signal;
  const entryTf = analysis?.timeframes?.M15;

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="logo">Au</span>
          <div>
            <h1>Gold Intelligence Trading System</h1>
            <p className="sub">
              XAU/USD · {status?.data_source || "…"} data · {status?.mode === "paper" ? "analysis + simulated trading" : `${status?.mode || "…"} mode`} · {analysis?.profile ? `${analysis.profile.name} (${analysis.profile.primary}/${analysis.profile.secondary} → ${analysis.profile.confirm} → ${analysis.profile.entry})` : "…"}
            </p>
          </div>
        </div>
        <div className="ticker">
          <div className="price">{fmt(analysis?.price)}</div>
          <div className="muted">spread {fmt(analysis?.spread)} · {analysis?.session || "—"}</div>
        </div>
        <div className="top-badges">
          <Badge tone={tone(dir?.label)}>{dir?.label || "—"} {dir ? `(${dir.score > 0 ? "+" : ""}${fmt(dir.score, 2)})` : ""}</Badge>
          <Badge tone={live ? "up" : "flat"}>{live ? "● live stream" : "○ polling"}</Badge>
          {status?.emergency_shutdown && <Badge tone="down">EMERGENCY STOP</Badge>}
          {analysis?.tier1?.blackout && <Badge tone="down">NFP/CPI WINDOW</Badge>}
        </div>
        <div className="controls">
          <div className="seg">
            {["analysis", "paper"].map((m) => (
              <button key={m} className={status?.mode === m ? "on" : ""} onClick={() => act(() => api.setMode(m), (r) => r.reason)}>{m}</button>
            ))}
            <button disabled title={`Live needs: ${(status?.live_checklist_missing || []).join(", ")}`}>live 🔒</button>
          </div>
          <button className="btn" onClick={() => act(api.markPaper, (r) => `${r.closed.length} paper positions closed`)}>Mark paper</button>
          {status?.emergency_shutdown
            ? <button className="btn" onClick={resetShutdown}>Reset shutdown</button>
            : <button className="btn danger" onClick={shutdown}>Emergency stop</button>}
          {"Notification" in window && Notification.permission === "default" && (
            <button className="btn" onClick={() => Notification.requestPermission()}>Enable alerts</button>
          )}
        </div>
      </header>

      {error && <div className="banner error">API unreachable: {error}</div>}
      {status?.data_source === "synthetic" && (
        <div className="banner">Demo mode: prices are a synthetic random path and the calendar/news are example rows. Load broker or CSV data before drawing conclusions.</div>
      )}
      {analysis?.data?.problems?.length > 0 && <div className="banner error">Data problems: {analysis.data.problems.join(" · ")}</div>}

      <main className="grid">
        <Card
          title="Price chart"
          subtitle={`${chartTf} · EMA 20/50 · signal levels and S/R overlaid`}
          className="span-8"
          actions={<div className="seg">{CHART_TFS.map((tf) => <button key={tf} className={tf === chartTf ? "on" : ""} onClick={() => setChartTf(tf)}>{tf}</button>)}</div>}
        >
          <PriceChart candles={candles} levels={signal?.actionable ? signal : null} zones={entryTf} />
        </Card>
        <div className="span-4"><SignalPanel evaluation={evaluation} /></div>
        <div className="span-7"><MtfTable analysis={analysis} /></div>
        <div className="span-5"><NfpCpiPanel analysis={analysis} /></div>
        <div className="span-7"><LiquidityPanel analysis={analysis} /></div>
        <div className="span-5"><IndicatorsPanel analysis={analysis} timeframe={analysis?.profile?.entry || "M15"} /></div>
        <div className="span-4"><RiskPanel risk={risk} onAckPause={ackPause} /></div>
        <div className="span-4"><CalendarPanel analysis={analysis} /></div>
        <div className="span-4"><NewsPanel analysis={analysis} /></div>
        <div className="span-12"><PerformancePanel performance={performance} backtest={backtest} learning={learning} onRunBacktest={runBacktest} running={running} /></div>
        <div className="span-12"><JournalPanel journal={journal} kind={journalKind} onKind={setJournalKind} /></div>
      </main>

      <footer className="foot">
        Analysis and paper-trading software. Signals carry evidence, confidence, and invalidation; none is a guarantee of profit.
        Live order routing stays locked until the operator checklist and a reviewed broker adapter are in place.
      </footer>

      <div className="toasts">
        {toasts.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.text}</div>)}
      </div>
    </div>
  );
}
