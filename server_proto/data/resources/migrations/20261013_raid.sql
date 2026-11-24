-- Additive old-save compatibility migration. No existing table or row is replaced.
CREATE TABLE IF NOT EXISTS raid_admission_receipts (
    instance_id TEXT PRIMARY KEY,
    family TEXT NOT NULL CHECK (family IN ('anton','luke')),
    manifest_sha256 TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS raid_daily_entries (
    account_id INTEGER NOT NULL,
    family TEXT NOT NULL CHECK (family IN ('anton','luke')),
    period_key TEXT NOT NULL,
    entry_count INTEGER NOT NULL CHECK (entry_count >= 0),
    PRIMARY KEY (account_id, family, period_key)
);
CREATE TABLE IF NOT EXISTS raid_reward_receipts (
    instance_id TEXT NOT NULL,
    phase INTEGER NOT NULL,
    kind INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    plan_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (instance_id, phase, kind, character_id)
);
