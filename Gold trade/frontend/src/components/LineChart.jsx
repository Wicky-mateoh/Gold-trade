import { useEffect, useRef } from "react";
import { createChart } from "lightweight-charts";

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Equity / drawdown area line. `points` are plain numbers indexed by sample.
export default function LineChart({ points, color = "--accent", height = 160 }) {
  const host = useRef(null);
  const refs = useRef({});

  useEffect(() => {
    const chart = createChart(host.current, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: cssVar("--muted"), fontFamily: "inherit" },
      grid: { vertLines: { visible: false }, horzLines: { color: cssVar("--grid") } },
      timeScale: { visible: false },
      rightPriceScale: { borderVisible: false },
      handleScroll: false,
      handleScale: false,
    });
    const line = cssVar(color);
    refs.current.series = chart.addAreaSeries({
      lineColor: line,
      topColor: line + "44",
      bottomColor: line + "00",
      lineWidth: 2,
      priceLineVisible: false,
    });
    refs.current.chart = chart;
    return () => chart.remove();
  }, [color]);

  useEffect(() => {
    if (!points?.length) return;
    refs.current.series.setData(points.map((value, i) => ({ time: i + 1, value })));
    refs.current.chart.timeScale().fitContent();
  }, [points]);

  return <div className="chart small" style={{ height }} ref={host} />;
}
