export const fmt = (v, d = 2) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
export const pct = (v, d = 2) => (v === null || v === undefined ? "—" : `${fmt(v, d)}%`);
export const money = (v) => (v === null || v === undefined ? "—" : `${v < 0 ? "-" : ""}$${fmt(Math.abs(v), 2)}`);
export const time = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().replace("T", " ").slice(0, 16) + "Z";
};
export const tone = (label) => ({ bullish: "up", BUY: "up", bearish: "down", SELL: "down" })[label] || "flat";
