-- portable:rebuild
--
-- Wash sales are detected in replay, across every account. ADR 0037.
--
-- Three parts:
--
--   `lot_disposition` and `realized_gain` are DROPPED and recreated empty.
--   Both are derived, so nothing is copied and `pt migrate` rebuilds them. The
--   disposition's `wash_sale_deferred` now means what its name says, and the
--   gain gains `wash_sale_disallowed`: the adjustment Form 8949 reports beside
--   the gain under code W. A NOT NULL column with no default cannot be added
--   to an existing table, and a default would let a writer omit it.
--
--   `wash_sale` is new and derived: one row per pairing of a sold lot's loss
--   with the replacement shares that disallow it.
--
--   `instrument_equivalence` is new reference state: the pairs of instruments
--   the owner has declared substantially identical. Never written by a
--   rebuild.
--
-- Valuation snapshots are kept (ADR 0024): they record no gain.

-- ── Derived state, emptied for the rebuild ──────────────────────────────────
-- The order of Repositories.clear_derived, which is the authority on what is
-- derived.
DELETE FROM realized_gain;
DELETE FROM lot_disposition;
DELETE FROM lot_basis_adjustment;
DELETE FROM lot;
DELETE FROM position_leg;
DELETE FROM position;
DELETE FROM cash_balance;

DROP TABLE realized_gain;
DROP TABLE lot_disposition;

-- @table lot_disposition
-- One row per (closing transaction, lot consumed). The tax engine's input.
CREATE TABLE lot_disposition (
    disposition_id  INTEGER PRIMARY KEY,
    lot_id          INTEGER NOT NULL REFERENCES lot (lot_id),
    txn_id          INTEGER NOT NULL REFERENCES "transaction" (txn_id),
    account_id      INTEGER NOT NULL REFERENCES account (account_id),
    instrument_id   INTEGER NOT NULL REFERENCES instrument (instrument_id),
    disposition_date TEXT NOT NULL,
    quantity        TEXT NOT NULL,  -- decimal
    -- Includes option premium where an assignment resolved into stock: a
    -- written call that is assigned adds its premium to PROCEEDS, not to
    -- independent P&L. For a cover, the short sale's credit (ADR 0038).
    proceeds        TEXT NOT NULL,  -- decimal
    allocated_fees  TEXT NOT NULL DEFAULT '0.00',  -- decimal
    cost_basis_relieved TEXT NOT NULL,  -- decimal
    -- Proceeds less basis, before any wash-sale adjustment.
    realized_gain   TEXT NOT NULL,  -- decimal
    -- Determined at disposition from the lot's holding_period_start. Short
    -- sales are always 'short'.
    holding_period  TEXT NOT NULL CHECK (holding_period IN ('short', 'long')),
    -- Days held, recorded so the boundary determination is auditable rather
    -- than having to be recomputed to be checked.
    days_held       INTEGER NOT NULL,
    relief_method   TEXT NOT NULL
        CHECK (relief_method IN ('spec', 'fifo', 'lifo', 'hifo', 'lofo', 'avg')),
    -- ADR 0037. The part of this loss a wash sale disallows: deferred into a
    -- taxable replacement's basis, or lost to a sheltered one. NULL when no
    -- replacement disallows any of it. A pairing reported for manual handling
    -- is not counted here: its amount is in `wash_sale` and the year is
    -- marked incomplete.
    wash_sale_deferred TEXT  -- decimal
);

CREATE INDEX IF NOT EXISTS ix_disposition_txn ON lot_disposition (txn_id);
CREATE INDEX IF NOT EXISTS ix_disposition_account_date
    ON lot_disposition (account_id, disposition_date);
CREATE INDEX IF NOT EXISTS ix_disposition_lot ON lot_disposition (lot_id);

-- @table realized_gain
-- Per-disposition realized gain with the estimated tax applied, materialized
-- for `pt tax` and `pt pnl --net-of-tax`.
--
-- The tax figure is an ESTIMATE: the allowed gain times the account's
-- effective-dated rate for the holding period at disposition. It does not
-- model brackets, the capital-loss limitation, carryforwards, rate stacking,
-- AMT, or any other income (ADR 0011). The components are stored separately
-- so the effective rate is explainable rather than a magic number.
CREATE TABLE realized_gain (
    realized_gain_id INTEGER PRIMARY KEY,
    disposition_id  INTEGER NOT NULL UNIQUE REFERENCES lot_disposition (disposition_id),
    account_id      INTEGER NOT NULL REFERENCES account (account_id),
    instrument_id   INTEGER NOT NULL REFERENCES instrument (instrument_id),
    tax_year        INTEGER NOT NULL,
    disposition_date TEXT NOT NULL,
    holding_period  TEXT NOT NULL CHECK (holding_period IN ('short', 'long')),
    proceeds        TEXT NOT NULL,  -- decimal
    cost_basis      TEXT NOT NULL,  -- decimal
    -- Proceeds less basis. The allowed gain is gain + wash_sale_disallowed.
    gain            TEXT NOT NULL,  -- decimal
    -- ADR 0037. The Form 8949 adjustment, code W: the part of a loss a wash
    -- sale disallows. '0.00' when none does, which is a stated figure.
    wash_sale_disallowed TEXT NOT NULL,  -- decimal
    -- The rate schedule row used, so the estimate can be traced.
    rate_id         INTEGER REFERENCES tax_rate_schedule (rate_id),
    federal_rate    TEXT,  -- decimal
    state_rate      TEXT,  -- decimal
    niit_rate       TEXT,  -- decimal
    estimated_tax   TEXT,  -- decimal
    -- 0 for tax_deferred and tax_exempt accounts, where the estimate is not
    -- merely zero but inapplicable; the flag says which.
    is_taxable      INTEGER NOT NULL CHECK (is_taxable IN (0, 1))
);

CREATE INDEX IF NOT EXISTS ix_realized_gain_year
    ON realized_gain (tax_year, account_id);

-- ── Pairings ────────────────────────────────────────────────────────────────
-- @table wash_sale
-- One row per pairing of a sold lot's loss with the replacement shares that
-- disallow it (ADR 0037). Derived: replay makes each pairing when the later
-- of the two rows is applied, so a rebuild and a live append agree.
CREATE TABLE IF NOT EXISTS wash_sale (
    wash_sale_id    INTEGER PRIMARY KEY,
    disposition_id  INTEGER NOT NULL REFERENCES lot_disposition (disposition_id),
    -- The loss sale, and the account it realized the loss in.
    txn_id          INTEGER NOT NULL REFERENCES "transaction" (txn_id),
    account_id      INTEGER NOT NULL REFERENCES account (account_id),
    sold_lot_key    TEXT NOT NULL,
    sale_date       TEXT NOT NULL,
    -- The replacement shares: the lot split off to carry them, or, for a
    -- pairing reported for manual handling, the lot they were bought into.
    replacement_lot_key TEXT NOT NULL,
    replacement_date TEXT NOT NULL,
    quantity        TEXT NOT NULL,  -- decimal
    -- The loss disallowed: the matched shares times the sold lot's loss per
    -- share. For a manual pairing, what would be disallowed.
    disallowed      TEXT NOT NULL,  -- decimal
    status          TEXT NOT NULL CHECK (status IN (
        'deferred',   -- added to a taxable replacement's basis
        'permanent',  -- a sheltered replacement: lost, not deferred
        'manual'      -- the replacement was disposed of before the loss sale
    )),
    -- Days of the sold shares' holding period the replacement absorbed.
    days_absorbed   INTEGER NOT NULL CHECK (days_absorbed >= 0)
);

CREATE INDEX IF NOT EXISTS ix_wash_sale_disposition ON wash_sale (disposition_id);
CREATE INDEX IF NOT EXISTS ix_wash_sale_replacement ON wash_sale (replacement_lot_key);

-- ── Declarations ────────────────────────────────────────────────────────────
-- @table instrument_equivalence
-- Pairs of instruments the owner has declared substantially identical, so
-- that a purchase of one is matched against a loss on the other (ADR 0037).
-- Never inferred. Reference state: never written by a rebuild.
CREATE TABLE IF NOT EXISTS instrument_equivalence (
    equivalence_id  INTEGER PRIMARY KEY,
    -- The lower instrument id first, so a pair is stored once.
    instrument_a_id INTEGER NOT NULL REFERENCES instrument (instrument_id),
    instrument_b_id INTEGER NOT NULL REFERENCES instrument (instrument_id),
    -- Why they are substantially identical. Required: a judgement with no
    -- stated reasoning is indistinguishable from a guess.
    reason          TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    created_at      TEXT NOT NULL,
    CHECK (instrument_a_id < instrument_b_id),
    UNIQUE (instrument_a_id, instrument_b_id)
);
