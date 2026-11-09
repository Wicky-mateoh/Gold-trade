import { useMemo, useState } from "react";
import Card, { Badge, Stat } from "./Card.jsx";
import LineChart from "./LineChart.jsx";
import { fmt, money, pct, time, tone } from "../format.js";

const TF_ORDER = ["MN1", "W1", "D1", "H4", "H1", "M15", "M5"];

export function SignalPanel({ evaluation }) {
  const s = evaluation?.signal;
  if (!s) return <Card title="Active signal"><p className="muted">Waiting for the first evaluation…</p></Card>;
  const actionable = s.actionable;
  const risk = evaluation.risk;
  return (
    <Card
      title="Active signal"
      subtitle={`${s.instrument} · ${time(s.timestamp)} · session ${s.session || "—"}`}
      className={`signal ${tone(s.decision)}`}
      actions={<Badge tone={tone(s.decision)}>{s.decision.replace("_", " ")}</Badge>}
    >
      <div className="stats">
        <Stat label="Price" value={fmt(s.price)} />
        <Stat label="Confidence" value={s.confidence == null ? "—" : `${fmt(s.confidence, 1)} / 100`} />
        <Stat label="Candidate" value={s.candidate_direction || "none"} tone={tone(s.candidate_direction)} />
        <Stat label="Reward : risk" value={s.reward_risk ? `${fmt(s.reward_risk)} R` : "—"} />
      </div>
      {actionable && (
        <div className="levels">
          <Stat label="Entry zone" value={`${fmt(s.entry_low)} – ${fmt(s.entry_high)}`} />
          <Stat label="Stop loss" value={fmt(s.stop_loss)} tone="down" />
          <Stat label="Take profit" value={(s.take_profits || []).map((t) => fmt(t)).join(" / ")} tone="up" />
          <Stat label="Risk gate" value={risk?.approved ? `approved · ${risk.lots} lots` : "rejected"} tone={risk?.approved ? "up" : "down"} />
        </div>
      )}
      <div className="cols">
        <div>
          <h3>{actionable ? "Evidence" : "Why no trade"}</h3>
          <ul className="reasons">{(actionable ? s.evidence : s.reasons).map((r, i) => <li key={i}>{r}</li>)}</ul>
          {actionable && risk && !risk.approved && (
            <>
              <h3>Risk rejections</h3>
              <ul className="reasons warn">{risk.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
            </>
          )}
        </div>
        <div>
          <h3>Fundamental context</h3>
          <ul className="reasons">{(s.fundamental_context || []).map((r, i) => <li key={i}>{r}</li>)}</ul>
          <h3>Invalidation</h3>
          <ul className="reasons">{(s.invalidation || []).map((r, i) => <li key={i}>{r}</li>)}</ul>
        </div>
      </div>
      {s.component_scores && Object.keys(s.component_scores).length > 0 && (
        <div className="bars">
          {Object.entries(s.component_scores).map(([k, v]) => (
            <div className="bar-row" key={k}>
              <span>{k.replace("_", " ")}</span>
              <div className="bar"><div style={{ width: `${Math.round(v * 100)}%` }} /></div>
              <span className="num">{fmt(v, 2)}</span>
            </div>
          ))}
        </div>
      )}
      <p className="fine">Confidence is a calibrated quality score, not a probability of profit. No order reaches a broker from this screen.</p>
    </Card>
  );
}

export function MtfTable({ analysis }) {
  const tfs = analysis?.timeframes || {};
  return (
    <Card title="Multi-timeframe analysis" subtitle="Higher frames set direction · lower frames time the entry">
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>TF</th><th>Role</th><th>Bias</th><th>Event</th><th>Regime</th><th>RSI</th><th>ADX</th><th>Agreement</th></tr>
          </thead>
          <tbody>
            {TF_ORDER.map((tf) => {
              const r = tfs[tf] || {};
              return (
                <tr key={tf}>
                  <td className="mono">{tf}</td>
                  <td className="muted">{r.role || "—"}</td>
                  <td><Badge tone={tone(r.bias)}>{r.bias || "n/a"}</Badge></td>
                  <td className="muted">{r.event ? r.event.replace("_", " ") : "—"}</td>
                  <td className="muted">{r.regime || "—"}</td>
                  <td className="num">{fmt(r.indicators?.rsi, 1)}</td>
                  <td className="num">{fmt(r.indicators?.adx, 1)}</td>
                  <td className={`num ${r.indicator_agreement > 0.15 ? "up" : r.indicator_agreement < -0.15 ? "down" : ""}`}>
                    {r.indicator_agreement == null ? "—" : (r.indicator_agreement > 0 ? "+" : "") + fmt(r.indicator_agreement, 2)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {analysis?.direction?.conflict && <p className="warn-line">D1 / H4 / H1 disagree. Signal confidence is reduced.</p>}
    </Card>
  );
}

export function IndicatorsPanel({ analysis, timeframe }) {
  const ind = analysis?.timeframes?.[timeframe]?.indicators || {};
  const rows = [
    ["EMA 20", ind.ema20], ["EMA 50", ind.ema50], ["EMA 100", ind.ema100], ["EMA 200", ind.ema200],
    ["RSI 14", ind.rsi, 1], ["MACD", ind.macd, 3], ["MACD signal", ind.macd_signal, 3], ["MACD hist", ind.macd_hist, 3],
    ["ATR 14", ind.atr], ["Bollinger upper", ind.bollinger_upper], ["Bollinger mid", ind.bollinger_mid], ["Bollinger lower", ind.bollinger_lower],
    ["ADX", ind.adx, 1], ["+DI", ind.plus_di, 1], ["−DI", ind.minus_di, 1], ["Volume ratio", ind.volume_ratio, 2],
  ];
  return (
    <Card title={`Technical indicators · ${timeframe}`} subtitle={`Volatility regime: ${analysis?.volatility || "—"}`}>
      <div className="kv-grid">
        {rows.map(([k, v, d]) => (
          <div className="kv" key={k}><span>{k}</span><span className="num">{fmt(v, d ?? 2)}</span></div>
        ))}
      </div>
      {analysis?.fibonacci && Object.keys(analysis.fibonacci).length > 0 && (
        <>
          <h3>Fibonacci ({analysis?.profile?.primary || "D1"} dealing range)</h3>
          <div className="chips">
            {Object.entries(analysis.fibonacci).map(([k, v]) => <span className="chip" key={k}>{k}: {fmt(v)}</span>)}
          </div>
        </>
      )}
    </Card>
  );
}

export function LiquidityPanel({ analysis }) {
  const smc = analysis?.smc || {};
  const entry = analysis?.timeframes?.M15 || {};
  const list = (items, render) => (items?.length ? items.map(render) : [<li key="none" className="muted">none</li>]);
  return (
    <Card title="Liquidity & structure · M15" subtitle="Context only. These never trigger a trade without confirmation.">
      <div className="stats">
        <Stat label="Premium / discount" value={smc.premium_discount || "—"} />
        <Stat label="Equilibrium" value={fmt(smc.equilibrium)} />
        <Stat label="Displacement" value={smc.displacement || "none"} tone={tone(smc.displacement)} />
        <Stat label="Structure shift" value={smc.structure_shift?.replace("_", " ") || "none"} />
      </div>
      <div className="cols three">
        <div>
          <h3>Buy-side liquidity</h3>
          <ul className="mini">{list(smc.buy_side_liquidity, (p, i) => <li key={i}>{fmt(p)}</li>)}</ul>
          <h3>Sell-side liquidity</h3>
          <ul className="mini">{list(smc.sell_side_liquidity, (p, i) => <li key={i}>{fmt(p)}</li>)}</ul>
        </div>
        <div>
          <h3>Fair value gaps</h3>
          <ul className="mini">{list(smc.fair_value_gaps, (g, i) => <li key={i} className={tone(g.direction)}>{g.direction} {fmt(g.low)}–{fmt(g.high)}</li>)}</ul>
          <h3>Sweeps</h3>
          <ul className="mini">{list(smc.sweeps, (s, i) => <li key={i}>{s.side.replace("_", " ")} @ {fmt(s.level)}</li>)}</ul>
        </div>
        <div>
          <h3>Order / breaker / mitigation</h3>
          <ul className="mini">
            {list([...(smc.order_blocks || []).map((b) => ({ ...b, role: "OB" })), ...(smc.breaker_blocks || []).map((b) => ({ ...b, role: "BB" })), ...(smc.mitigation_blocks || []).map((b) => ({ ...b, role: "MB" }))].slice(-6),
              (b, i) => <li key={i} className={tone(b.direction)}>{b.role} {b.direction} {fmt(b.low)}–{fmt(b.high)}</li>)}
          </ul>
          <h3>Support / resistance</h3>
          <ul className="mini">
            <li>S: {(entry.support || []).map((p) => fmt(p)).join(", ") || "—"}</li>
            <li>R: {(entry.resistance || []).map((p) => fmt(p)).join(", ") || "—"}</li>
          </ul>
        </div>
      </div>
    </Card>
  );
}

export function CalendarPanel({ analysis }) {
  const macro = analysis?.macro || {};
  return (
    <Card title="Economic calendar" subtitle="High-impact releases pause new risk. No directional assumption is made." actions={macro.blackout ? <Badge tone="down">BLACKOUT</Badge> : <Badge>clear</Badge>}>
      <ul className="events">
        {[...(macro.active_events || []).map((e) => ({ ...e, active: true })), ...(macro.upcoming || [])].map((e, i) => (
          <li key={i} className={e.active ? "active" : ""}>
            <span className="mono">{time(e.timestamp)}</span>
            <span>{e.name}{e.tier1 && <span className="tag">{e.kind.toUpperCase()}</span>}{e.confirmed === false && <span className="tag dim">estimated</span>}</span>
            <Badge tone={e.impact === "high" ? "down" : "flat"}>{e.impact}</Badge>
          </li>
        ))}
        {!macro.upcoming?.length && !macro.active_events?.length && <li className="muted">No events loaded.</li>}
      </ul>
      {(macro.notes || []).map((n, i) => <p className="fine" key={i}>{n}</p>)}
    </Card>
  );
}

const nyTime = (iso) =>
  iso
    ? new Date(iso).toLocaleString("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) + " NY"
    : "—";

const countdown = (minutes) => {
  if (minutes === null || minutes === undefined) return "—";
  if (minutes < 0) return "released";
  const d = Math.floor(minutes / 1440);
  const h = Math.floor((minutes % 1440) / 60);
  const m = minutes % 60;
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
};

export function NfpCpiPanel({ analysis }) {
  const t = analysis?.tier1;
  if (!t) return <Card title="NFP & CPI monitor"><p className="muted">Loading…</p></Card>;
  const p = t.policy || {};
  const state = t.flatten_due ? ["CLOSING POSITIONS", "down"] : t.blackout ? ["NO NEW TRADES", "down"] : ["clear", "up"];
  return (
    <Card
      title="NFP & CPI monitor"
      subtitle={`No new trades ${p.blackout_before_min}m before → ${p.blackout_after_min}m after · open trades close ${p.flatten_before_min}m before release`}
      actions={<Badge tone={state[1]}>{state[0]}</Badge>}
    >
      <div className="tier1">
        {["nfp", "cpi"].map((k) => {
          const r = t[k];
          if (!r) return null;
          const n = r.next;
          return (
            <div className={`tier1-row ${r.in_blackout ? "hot" : ""}`} key={k}>
              <div className="tier1-head">
                <strong>{k.toUpperCase()}</strong>
                <span className="muted">{r.label}</span>
                {n && <Badge tone={r.confirmed ? "up" : "flat"}>{r.confirmed ? "confirmed" : "estimated"}</Badge>}
                {r.in_blackout && <Badge tone="down">window active</Badge>}
              </div>
              <div className="tier1-body">
                <div className="countdown">{countdown(r.minutes_until)}</div>
                <div>
                  <div className="mono">{nyTime(n?.timestamp)}</div>
                  <div className="muted mono">{n ? n.timestamp.slice(0, 16).replace("T", " ") + "Z" : ""} · {n?.source?.replaceAll("_", " ")}</div>
                </div>
              </div>
              {r.candidate_dates?.length > 1 && (
                <div className="fine">
                  Date not confirmed yet — all candidate days are protected: {r.candidate_dates.map((d) => new Date(d).toLocaleDateString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric" })).join(", ")}
                </div>
              )}
              {n && (n.forecast || n.previous) && (
                <div className="fine">Forecast {n.forecast ?? "—"} · previous {n.previous ?? "—"}</div>
              )}
              {r.last_release && (
                <div className="fine">Last: {r.last_release.name} · actual {r.last_release.actual ?? "—"} vs forecast {r.last_release.forecast ?? "—"}</div>
              )}
            </div>
          );
        })}
      </div>
      <p className="fine">Feed: {t.feed_status}. {p.rule}</p>
    </Card>
  );
}

export function NewsPanel({ analysis }) {
  const s = analysis?.sentiment || {};
  return (
    <Card title="News sentiment" subtitle={`${s.usable_count ?? 0} usable of ${s.recent_count ?? 0} recent items · ${s.lookback_hours ?? 48}h window`} actions={<Badge tone={tone(s.label)}>{s.label || "—"} {s.confidence ? fmt(s.confidence, 2) : ""}</Badge>}>
      <ul className="news">
        {(s.items || []).slice().reverse().slice(0, 8).map((n, i) => (
          <li key={i} className={n.usable_as_evidence ? "" : "excluded"}>
            <div className="news-head">
              <Badge tone={tone(n.label)}>{n.label}</Badge>
              <span className="muted">{n.source} · rel {fmt(n.source_reliability, 2)} · {time(n.published_at)}</span>
            </div>
            <div>{n.headline}</div>
            {!n.usable_as_evidence && <div className="fine">Excluded from evidence: {n.excluded_reason}</div>}
          </li>
        ))}
        {!s.items?.length && <li className="muted">No news loaded.</li>}
      </ul>
    </Card>
  );
}

export function RiskPanel({ risk, onAckPause }) {
  if (!risk) return <Card title="Risk exposure"><p className="muted">Loading…</p></Card>;
  const lim = risk.limits || {};
  const gauge = (label, value, limit) => {
    const ratio = Math.min(1, (value || 0) / (limit || 1));
    return (
      <div className="gauge" key={label}>
        <div className="gauge-head"><span>{label}</span><span className="num">{pct(value)} / {pct(limit)}</span></div>
        <div className="bar"><div className={ratio > 0.8 ? "hot" : ""} style={{ width: `${ratio * 100}%` }} /></div>
      </div>
    );
  };
  return (
    <Card title="Risk exposure & drawdown" subtitle="Independent of the signal engine · limits are operator-controlled">
      <div className="stats">
        <Stat label="Equity" value={money(risk.equity)} />
        <Stat label="Daily P&L" value={money(risk.daily_pnl)} tone={risk.daily_pnl < 0 ? "down" : risk.daily_pnl > 0 ? "up" : ""} />
        <Stat label="Weekly P&L" value={money(risk.weekly_pnl)} tone={risk.weekly_pnl < 0 ? "down" : risk.weekly_pnl > 0 ? "up" : ""} />
        <Stat label="Open positions" value={`${risk.open_positions.length} / ${lim.max_open_positions}`} />
      </div>
      {gauge("Daily drawdown", risk.daily_drawdown_pct, lim.max_daily_loss_pct)}
      {gauge("Weekly drawdown", risk.weekly_drawdown_pct, lim.max_weekly_drawdown_pct)}
      {gauge("Open risk", risk.open_risk_pct, lim.max_correlated_exposure_pct)}
      <div className="gauge">
        <div className="gauge-head"><span>Consecutive losses</span><span className="num">{risk.consecutive_losses} / {lim.max_consecutive_losses}</span></div>
      </div>
      {risk.loss_pause && (
        <div className="alert">
          Loss-streak pause is active. Size will not increase after acknowledgement.
          <button className="btn small" onClick={onAckPause}>Acknowledge</button>
        </div>
      )}
      {risk.open_positions.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>ID</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Lots</th></tr></thead>
            <tbody>
              {risk.open_positions.map((p) => (
                <tr key={p.id}><td className="mono">{p.id}</td><td className={tone(p.direction)}>{p.direction}</td><td className="num">{fmt(p.entry)}</td><td className="num">{fmt(p.stop_loss)}</td><td className="num">{fmt(p.take_profit)}</td><td className="num">{p.lots}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="chips">
        <span className="chip">risk/trade {pct(lim.max_risk_per_trade_pct)}</span>
        <span className="chip">min R:R {lim.min_reward_risk}</span>
        <span className="chip">max spread {lim.max_spread}</span>
        <span className="chip">sessions: {lim.allowed_sessions}</span>
      </div>
    </Card>
  );
}

export function PerformancePanel({ performance, backtest, learning, onRunBacktest, running }) {
  const m = backtest?.metrics;
  const curve = backtest?.equity_curve || [];
  const drawdown = useMemo(() => {
    let peak = -Infinity;
    return curve.map((v) => { peak = Math.max(peak, v); return peak ? -((peak - v) / peak) * 100 : 0; });
  }, [curve]);
  const oos = backtest?.out_of_sample;
  const paper = performance?.paper || {};
  return (
    <Card
      title="Performance & validation"
      subtitle={backtest?.timeframe ? `Backtest ${backtest.timeframe} on ${backtest.data_source} data · costs included` : "Run a backtest to populate"}
      actions={<button className="btn" disabled={running} onClick={onRunBacktest}>{running ? "Running…" : "Run backtest"}</button>}
    >
      <div className="stats">
        <Stat label="Paper closed" value={paper.closed ?? 0} />
        <Stat label="Paper win rate" value={paper.win_rate == null ? "—" : pct(paper.win_rate * 100, 1)} />
        <Stat label="Paper net" value={money(paper.net)} />
        <Stat label="Decisions logged" value={Object.values(performance?.decision_counts || {}).reduce((a, b) => a + b, 0)} />
      </div>
      {m && (
        <>
          <div className="kv-grid">
            {[
              ["Trades", m.trades, 0], ["Win rate", m.win_rate == null ? null : m.win_rate * 100, 1, "%"], ["Profit factor", m.profit_factor, 2],
              ["Net return", m.net_return_pct, 2, "%"], ["Max drawdown", m.max_drawdown_pct, 2, "%"], ["Avg R:R", m.average_reward_risk, 2],
              ["Sharpe", m.sharpe, 2], ["Sortino", m.sortino, 2], ["Expectancy", m.expectancy, 2, "$"],
              ["Max win streak", m.consecutive_wins, 0], ["Max loss streak", m.consecutive_losses, 0], ["Monte Carlo p95 DD", backtest.monte_carlo?.max_drawdown_p95, 2, "$"],
            ].map(([k, v, d, unit]) => (
              <div className="kv" key={k}><span>{k}</span><span className="num">{v == null ? "—" : unit === "$" ? money(v) : `${fmt(v, d)}${unit || ""}`}</span></div>
            ))}
          </div>
          <div className="cols">
            <div><h3>Equity curve</h3><LineChart points={curve} color="--accent" /></div>
            <div><h3>Drawdown %</h3><LineChart points={drawdown} color="--down" /></div>
          </div>
          {oos && (
            <>
              <h3>Walk-forward · <Badge tone={oos.passed ? "up" : "down"}>{oos.passed ? "passed" : "not passed"}</Badge></h3>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>#</th><th>Role</th><th>Window</th><th>Trades</th><th>Net %</th><th>PF</th><th>Max DD %</th></tr></thead>
                  <tbody>
                    {oos.segments.map((s) => (
                      <tr key={s.segment}><td>{s.segment}</td><td className="muted">{s.role.replaceAll("_", " ")}</td><td className="mono">{s.start.slice(0, 10)} → {s.end.slice(0, 10)}</td><td className="num">{s.trades}</td><td className={`num ${s.net_return_pct > 0 ? "up" : s.net_return_pct < 0 ? "down" : ""}`}>{fmt(s.net_return_pct)}</td><td className="num">{fmt(s.profit_factor)}</td><td className="num">{fmt(s.max_drawdown_pct)}</td></tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <ul className="reasons">{oos.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
            </>
          )}
          {(m.by_session && Object.keys(m.by_session).length > 0) && (
            <div className="chips">
              {Object.entries(m.by_session).map(([k, v]) => <span className="chip" key={k}>{k}: {v.trades} trades · {money(v.net)}</span>)}
              {Object.entries(m.by_regime || {}).map(([k, v]) => <span className="chip" key={`r-${k}`}>{k}: {v.trades} · {money(v.net)}</span>)}
            </div>
          )}
        </>
      )}
      {learning?.calibration?.length > 0 && (
        <>
          <h3>Historical signal accuracy (calibration)</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Confidence</th><th>Samples</th><th>Avg conf.</th><th>Realized win %</th><th></th></tr></thead>
              <tbody>
                {learning.calibration.map((r) => (
                  <tr key={r.bucket}><td>{r.bucket}</td><td className="num">{r.samples}</td><td className="num">{fmt(r.average_confidence, 1)}</td><td className="num">{fmt(r.realized_win_rate_pct, 1)}</td><td>{r.overconfident && <Badge tone="down">overconfident</Badge>}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {learning?.drift?.drift && <p className="warn-line">Model drift detected: {learning.drift.action}</p>}
      <p className="fine">{backtest?.deployment_gate?.statement || "Backtest results are research evidence, not a promise of future returns."}</p>
    </Card>
  );
}

const KINDS = ["", "signal", "rejected_setup", "risk_decision", "execution", "mode_change", "emergency", "model_version"];

export function JournalPanel({ journal, kind, onKind }) {
  const [open, setOpen] = useState(null);
  return (
    <Card title="Trading journal" subtitle="Append-only audit of every decision, rejection, and execution" actions={
      <select value={kind} onChange={(e) => onKind(e.target.value)}>
        {KINDS.map((k) => <option key={k} value={k}>{k ? k.replace("_", " ") : "all"}</option>)}
      </select>
    }>
      <div className="table-wrap tall">
        <table>
          <thead><tr><th>Time</th><th>Kind</th><th>Summary</th></tr></thead>
          <tbody>
            {(journal || []).map((row) => {
              const p = row.payload || {};
              const summary = p.decision ? `${p.decision}${p.candidate_direction ? ` (candidate ${p.candidate_direction})` : ""} · conf ${p.confidence ?? "—"} · ${(p.reasons || [])[0] || ""}`
                : p.event ? `${p.event} ${p.direction || ""} ${p.pnl != null ? money(p.pnl) : ""}`
                : p.approved !== undefined ? `${p.approved ? "approved" : "rejected"} · ${(p.reasons || [])[0] || ""}`
                : p.mode ? `mode ${p.mode}` : p.version ? `model ${p.version}` : JSON.stringify(p).slice(0, 90);
              return (
                <tr key={row.id} onClick={() => setOpen(open === row.id ? null : row.id)} className="clickable">
                  <td className="mono">{time(row.recorded_at)}</td>
                  <td><Badge tone={row.kind === "signal" ? "up" : row.kind === "emergency" ? "down" : "flat"}>{row.kind.replace("_", " ")}</Badge></td>
                  <td>{summary}{open === row.id && <pre>{JSON.stringify(p, null, 2)}</pre>}</td>
                </tr>
              );
            })}
            {!journal?.length && <tr><td colSpan={3} className="muted">No journal rows yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
