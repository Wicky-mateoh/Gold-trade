import { useEffect, useRef } from "react";
import { createChart, CrosshairMode, LineStyle } from "lightweight-charts";

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const toUnix = (iso) => Math.floor(new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z").getTime() / 1000);

// TradingView lightweight-charts candles with EMA overlays, signal levels, and S/R.
export default function PriceChart({ candles, levels, zones }) {
  const host = useRef(null);
  const refs = useRef({ lines: [] });

  useEffect(() => {
    const chart = createChart(host.current, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: cssVar("--muted"), fontFamily: "inherit" },
      grid: { vertLines: { color: cssVar("--grid") }, horzLines: { color: cssVar("--grid") } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: cssVar("--line") },
      timeScale: { borderColor: cssVar("--line"), timeVisible: true },
    });
    refs.current.candles = chart.addCandlestickSeries({
      upColor: cssVar("--up"),
      downColor: cssVar("--down"),
      borderVisible: false,
      wickUpColor: cssVar("--up"),
      wickDownColor: cssVar("--down"),
    });
    refs.current.ema20 = chart.addLineSeries({ color: cssVar("--accent"), lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    refs.current.ema50 = chart.addLineSeries({ color: cssVar("--muted"), lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    refs.current.chart = chart;
    return () => chart.remove();
  }, []);

  useEffect(() => {
    if (!candles?.length) return;
    const rows = candles.map((r) => ({ time: toUnix(r.time), open: r.open, high: r.high, low: r.low, close: r.close }));
    refs.current.candles.setData(rows);
    const ema = (period) => {
      const alpha = 2 / (period + 1);
      let prev = null;
      const out = [];
      rows.forEach((r, i) => {
        prev = prev === null ? r.close : alpha * r.close + (1 - alpha) * prev;
        if (i >= period - 1) out.push({ time: r.time, value: prev });
      });
      return out;
    };
    refs.current.ema20.setData(ema(20));
    refs.current.ema50.setData(ema(50));
  }, [candles]);

  useEffect(() => {
    const series = refs.current.candles;
    if (!series) return;
    refs.current.lines.forEach((line) => series.removePriceLine(line));
    const lines = [];
    const add = (price, title, color, style = LineStyle.Solid) => {
      if (price === null || price === undefined) return;
      lines.push(series.createPriceLine({ price, title, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true }));
    };
    if (levels) {
      add(levels.entry_low, "entry", cssVar("--accent"), LineStyle.Dotted);
      add(levels.entry_high, "entry", cssVar("--accent"), LineStyle.Dotted);
      add(levels.stop_loss, "SL", cssVar("--down"));
      (levels.take_profits || []).forEach((tp, i) => add(tp, `TP${i + 1}`, cssVar("--up")));
    }
    (zones?.support || []).slice(-2).forEach((p) => add(p, "S", cssVar("--muted"), LineStyle.Dashed));
    (zones?.resistance || []).slice(-2).forEach((p) => add(p, "R", cssVar("--muted"), LineStyle.Dashed));
    refs.current.lines = lines;
  }, [levels, zones]);

  return <div className="chart" ref={host} />;
}
