export default function Card({ title, subtitle, children, className = "", actions }) {
  return (
    <section className={`card ${className}`}>
      <header className="card-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p className="sub">{subtitle}</p>}
        </div>
        {actions && <div className="card-actions">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

export function Badge({ children, tone = "flat" }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function Stat({ label, value, tone }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${tone || ""}`}>{value}</span>
    </div>
  );
}
