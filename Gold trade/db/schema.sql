-- Gold Intelligence Trading System - PostgreSQL schema.
-- The application journal mirrors every row into `journal`. The typed tables
-- below are for reporting and are populated by ETL from the journal or by
-- future writers; the journal remains the source of truth.

CREATE TABLE IF NOT EXISTS journal (
    id           UUID PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN (
                   'signal','rejected_setup','risk_decision','execution',
                   'mode_change','emergency','model_version','operator_note')),
    recorded_at  TIMESTAMPTZ NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    payload      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_kind_time ON journal (kind, recorded_at DESC);

CREATE TABLE IF NOT EXISTS candles (
    instrument  TEXT NOT NULL,
    timeframe   TEXT NOT NULL CHECK (timeframe IN ('MN1','W1','D1','H4','H1','M15','M5')),
    time        TIMESTAMPTZ NOT NULL,
    open        NUMERIC(12,3) NOT NULL,
    high        NUMERIC(12,3) NOT NULL,
    low         NUMERIC(12,3) NOT NULL,
    close       NUMERIC(12,3) NOT NULL,
    volume      NUMERIC(18,2) NOT NULL DEFAULT 0,
    spread      NUMERIC(8,3),
    source      TEXT NOT NULL,
    PRIMARY KEY (instrument, timeframe, time),
    CHECK (high >= GREATEST(open, close) AND low <= LEAST(open, close) AND high >= low)
);

CREATE TABLE IF NOT EXISTS signals (
    id              UUID PRIMARY KEY,
    instrument      TEXT NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL,
    decision        TEXT NOT NULL CHECK (decision IN ('BUY','SELL','NO_TRADE')),
    candidate       TEXT,
    price           NUMERIC(12,3),
    entry_low       NUMERIC(12,3),
    entry_high      NUMERIC(12,3),
    stop_loss       NUMERIC(12,3),
    take_profits    NUMERIC(12,3)[],
    reward_risk     NUMERIC(6,2),
    confidence      NUMERIC(5,1),
    evidence        JSONB NOT NULL DEFAULT '[]',
    fundamentals    JSONB NOT NULL DEFAULT '[]',
    invalidation    JSONB NOT NULL DEFAULT '[]',
    reasons         JSONB NOT NULL DEFAULT '[]',
    component_scores JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS signals_time ON signals (decided_at DESC);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id          UUID PRIMARY KEY,
    signal_id   UUID REFERENCES signals(id),
    decided_at  TIMESTAMPTZ NOT NULL,
    approved    BOOLEAN NOT NULL,
    lots        NUMERIC(10,2) NOT NULL DEFAULT 0,
    risk_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    risk_pct    NUMERIC(6,3) NOT NULL,
    reasons     JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    id          UUID PRIMARY KEY,
    position_id TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('paper','live')),
    event       TEXT NOT NULL CHECK (event IN ('open','close','stop_update','emergency_flatten')),
    direction   TEXT NOT NULL CHECK (direction IN ('BUY','SELL')),
    price       NUMERIC(12,3),
    lots        NUMERIC(10,2),
    pnl         NUMERIC(14,2),
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS economic_events (
    id          BIGSERIAL PRIMARY KEY,
    scheduled_at TIMESTAMPTZ NOT NULL,
    name        TEXT NOT NULL,
    impact      TEXT NOT NULL CHECK (impact IN ('low','medium','high','holiday')),
    category    TEXT NOT NULL,
    source      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_items (
    id            BIGSERIAL PRIMARY KEY,
    published_at  TIMESTAMPTZ NOT NULL,
    source        TEXT NOT NULL,
    reliability   NUMERIC(3,2) NOT NULL,
    importance    TEXT NOT NULL,
    headline      TEXT NOT NULL,
    label         TEXT NOT NULL CHECK (label IN ('bullish','bearish','neutral','uncertain')),
    confidence    NUMERIC(4,3) NOT NULL,
    usable        BOOLEAN NOT NULL
);

-- Model versions are append-only. Promotion is a new row, never an UPDATE of metrics.
CREATE TABLE IF NOT EXISTS model_versions (
    id          BIGSERIAL PRIMARY KEY,
    version     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('candidate','draft','validated','retired')),
    metrics     JSONB NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Risk-limit changes are operator actions and are audited here.
CREATE TABLE IF NOT EXISTS risk_limit_changes (
    id          BIGSERIAL PRIMARY KEY,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    operator    TEXT NOT NULL,
    key         TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT NOT NULL,
    reason      TEXT NOT NULL
);
