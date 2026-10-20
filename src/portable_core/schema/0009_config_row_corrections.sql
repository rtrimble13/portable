-- portable:rebuild
--
-- A config row corrected on its own date. ADR 0025, the question it left open,
-- decided by the owner (2026-10-20): a superseding row.
--
-- A tax rate schedule or return policy is effective-dated and never edited,
-- and one date took one row. So a rate mistyped for 2024-01-01 could only be
-- overridden from 2024-01-02, and a sale on 2024-01-01 kept the wrong rate.
-- Now a second row for the same date may name the row it corrects. The old row
-- stays, so the history shows what was entered, what replaced it and when; every
-- lookup reads the row nothing corrects.
--
-- A correction may itself be corrected, so the rows of one date form a chain;
-- the two partial unique indexes below keep it a chain: one original per date,
-- and each row corrected at most once. A trigger keeps a correction to the same
-- account and date as the row it corrects.
--
-- SQLite cannot drop a UNIQUE constraint, so both tables are REBUILT, keeping
-- every row and every column, named one by one (ADR 0019, ADR 0022). Ids are
-- copied, so realized_gain.rate_id keeps pointing at the same schedules.

-- ── tax_rate_schedule ───────────────────────────────────────────────────────
CREATE TABLE tax_rate_schedule_new (
    rate_id         INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES account (account_id),
    effective_from  TEXT NOT NULL,
    -- Decimal fractions: 37% is '0.37'.
    short_term_federal TEXT NOT NULL,  -- decimal
    long_term_federal  TEXT NOT NULL,  -- decimal
    state              TEXT NOT NULL DEFAULT '0',  -- decimal
    niit               TEXT NOT NULL DEFAULT '0',  -- decimal
    -- Rate applied to qualified dividends, where it differs from long-term.
    qualified_dividend TEXT,  -- decimal
    note            TEXT,
    created_at      TEXT NOT NULL,
    -- The schedule of the same account and date this one corrects (ADR 0025).
    -- NULL for an original. A corrected row is kept and no longer read.
    corrects_rate_id INTEGER REFERENCES tax_rate_schedule (rate_id)
);

INSERT INTO tax_rate_schedule_new (
    rate_id, account_id, effective_from, short_term_federal, long_term_federal,
    state, niit, qualified_dividend, note, created_at
)
SELECT
    rate_id, account_id, effective_from, short_term_federal, long_term_federal,
    state, niit, qualified_dividend, note, created_at
FROM tax_rate_schedule;

DROP TABLE tax_rate_schedule;
ALTER TABLE tax_rate_schedule_new RENAME TO tax_rate_schedule;

-- One original per account and date, as the old UNIQUE constraint said.
CREATE UNIQUE INDEX IF NOT EXISTS ux_tax_rate_schedule_original
    ON tax_rate_schedule (account_id, effective_from) WHERE corrects_rate_id IS NULL;
-- Each row corrected at most once, so the rows of a date form one chain.
CREATE UNIQUE INDEX IF NOT EXISTS ux_tax_rate_schedule_corrects
    ON tax_rate_schedule (corrects_rate_id) WHERE corrects_rate_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_tax_rate_correction_same_key
BEFORE INSERT ON tax_rate_schedule
WHEN NEW.corrects_rate_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM tax_rate_schedule o
    WHERE o.rate_id = NEW.corrects_rate_id
      AND o.account_id = NEW.account_id
      AND o.effective_from = NEW.effective_from
)
BEGIN
    SELECT RAISE(ABORT, 'a correction names a schedule of the same account and date');
END;

-- ── return_policy ───────────────────────────────────────────────────────────
CREATE TABLE return_policy_new (
    policy_id       INTEGER PRIMARY KEY,
    effective_from  TEXT NOT NULL,
    large_flow_basis TEXT NOT NULL CHECK (large_flow_basis IN ('amount', 'percent')),
    large_flow_value TEXT NOT NULL,  -- decimal
    significant_flow_basis TEXT
        CHECK (significant_flow_basis IS NULL OR significant_flow_basis IN ('amount', 'percent')),
    significant_flow_value TEXT,  -- decimal
    materiality_return_bps TEXT,  -- decimal
    materiality_value      TEXT,  -- decimal
    risk_measure_basis TEXT NOT NULL DEFAULT 'gross_of_fees'
        CHECK (risk_measure_basis IN
            ('gross_of_fees', 'net_of_external_costs_only', 'net_of_fees')),
    note            TEXT,
    created_at      TEXT NOT NULL,
    -- The policy of the same date this one corrects (ADR 0025). NULL for an
    -- original. A corrected row is kept and no longer read.
    corrects_policy_id INTEGER REFERENCES return_policy (policy_id),
    CHECK ((significant_flow_basis IS NULL) = (significant_flow_value IS NULL))
);

INSERT INTO return_policy_new (
    policy_id, effective_from, large_flow_basis, large_flow_value,
    significant_flow_basis, significant_flow_value, materiality_return_bps,
    materiality_value, risk_measure_basis, note, created_at
)
SELECT
    policy_id, effective_from, large_flow_basis, large_flow_value,
    significant_flow_basis, significant_flow_value, materiality_return_bps,
    materiality_value, risk_measure_basis, note, created_at
FROM return_policy;

DROP TABLE return_policy;
ALTER TABLE return_policy_new RENAME TO return_policy;

CREATE UNIQUE INDEX IF NOT EXISTS ux_return_policy_original
    ON return_policy (effective_from) WHERE corrects_policy_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_return_policy_corrects
    ON return_policy (corrects_policy_id) WHERE corrects_policy_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_return_policy_correction_same_key
BEFORE INSERT ON return_policy
WHEN NEW.corrects_policy_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM return_policy o
    WHERE o.policy_id = NEW.corrects_policy_id
      AND o.effective_from = NEW.effective_from
)
BEGIN
    SELECT RAISE(ABORT, 'a correction names a policy of the same date');
END;
