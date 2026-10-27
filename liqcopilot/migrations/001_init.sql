-- Money is integer paise (1 crore = 10^9 paise), never float. The API speaks crores.
CREATE TABLE participants (
    bic TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    balance_paise BIGINT NOT NULL,
    reserve_floor_paise BIGINT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The RTGS queue: payments wait here until a settlement run settles them.
CREATE TABLE queued_payments (
    payment_id TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    receiver TEXT NOT NULL,
    amount_paise BIGINT NOT NULL,
    priority TEXT NOT NULL,
    arrival_order BIGINT NOT NULL,
    value_time TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    settled_run TEXT
);
CREATE INDEX queued_payments_status ON queued_payments (status, arrival_order);

-- One row per settlement run; result holds the engine's report (amounts as decimal strings).
CREATE TABLE settlement_runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    engine TEXT NOT NULL,
    queued_count BIGINT NOT NULL,
    settled_count BIGINT NOT NULL,
    settled_paise BIGINT NOT NULL,
    result TEXT NOT NULL
);
