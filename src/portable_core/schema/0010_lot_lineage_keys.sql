-- portable:rebuild
--
-- A lot is designated by its lineage, not by its row number. ADR 0036.
--
-- A specific-identification sale recorded its lots as `lot_selection =
-- '2:10'`: lot ids, which every rebuild renumbers. A back-dated lot-opening row
-- or a reversal therefore re-pointed earlier sales at other lots, silently.
-- Two parts:
--
--   `lot` is DROPPED and recreated empty, with `lot_key`: the lot's lineage,
--   derived by replay and unique in the file. Lots are derived state, so
--   nothing is copied and `pt migrate` rebuilds them. A NOT NULL column with
--   no default cannot be added to an existing table, and a default would let
--   a writer create a lot without a lineage.
--
--   `lot_designation` is new: what a designation written before schema 10
--   means, by key. The ledger row keeps its text (invariant 2). `pt migrate`
--   fills the table by replaying, and the owner decides the rows it cannot.
--
-- Valuation snapshots are kept (ADR 0024): they record no lot.

-- ── Derived state that hangs off a lot, then the rest of it ────────────────
-- The order of Repositories.clear_derived, which is the authority on what is
-- derived. The file has no derived state until `pt migrate` rebuilds it, and
-- `pt validate` says so: a reported, recoverable state (invariant 9).
DELETE FROM realized_gain;
DELETE FROM lot_disposition;
DELETE FROM lot_basis_adjustment;
DELETE FROM lot;
DELETE FROM position_leg;
DELETE FROM position;
DELETE FROM cash_balance;

-- ── The lot ─────────────────────────────────────────────────────────────────
DROP TABLE lot;

CREATE TABLE lot (
    lot_id          INTEGER PRIMARY KEY,
    leg_id          INTEGER NOT NULL REFERENCES position_leg (leg_id),
    position_id     INTEGER NOT NULL REFERENCES position (position_id),
    instrument_id   INTEGER NOT NULL REFERENCES instrument (instrument_id),
    account_id      INTEGER NOT NULL REFERENCES account (account_id),
    open_date       TEXT NOT NULL,
    open_txn_id     INTEGER NOT NULL REFERENCES "transaction" (txn_id),
    -- ADR 0036. The lot's lineage: the transaction that opened it ('1234'),
    -- or the action that derived it and the parent's key ('1500.1234'). No
    -- row number enters it, so no later row can change it, and a designation
    -- naming it means the same lot after every rebuild.
    lot_key         TEXT NOT NULL UNIQUE,
    original_quantity  TEXT NOT NULL,  -- decimal
    remaining_quantity TEXT NOT NULL,  -- decimal
    per_unit_price     TEXT NOT NULL,  -- decimal
    allocated_fees     TEXT NOT NULL DEFAULT '0.00',  -- decimal
    original_cost_basis TEXT NOT NULL,  -- decimal
    adjusted_cost_basis TEXT NOT NULL,  -- decimal
    holding_period_start TEXT NOT NULL,
    is_short        INTEGER NOT NULL DEFAULT 0 CHECK (is_short IN (0, 1)),
    status          TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'partial', 'closed')),
    closed_date     TEXT,
    -- ADR 0017 §2, unchanged from schema 3.
    basis_source    TEXT NOT NULL CHECK (basis_source IN (
        'derived', 'reconstructed', 'estimated', 'unavailable', 'custodian_asserted'
    )),
    basis_assumption TEXT,
    CHECK (basis_source <> 'derived' OR basis_assumption IS NULL),
    CHECK (
        basis_source NOT IN ('reconstructed', 'estimated', 'unavailable')
        OR basis_assumption IS NOT NULL
    ),
    CHECK (status <> 'closed' OR closed_date IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_lot_leg ON lot (leg_id, status);
CREATE INDEX IF NOT EXISTS ix_lot_instrument_account
    ON lot (instrument_id, account_id, status);
CREATE INDEX IF NOT EXISTS ix_lot_open_date ON lot (open_date);
CREATE INDEX IF NOT EXISTS ix_lot_basis_source ON lot (basis_source);

-- ── What an old designation means ───────────────────────────────────────────
-- @table lot_designation
-- One row per ledger row whose `lot_selection` predates schema 10 and so names
-- lot ids. Reference state: written by `pt migrate`, `pt import` and the
-- owner's decision, never by a rebuild (ADR 0010, ADR 0036).
CREATE TABLE IF NOT EXISTS lot_designation (
    txn_id          INTEGER PRIMARY KEY REFERENCES "transaction" (txn_id),
    -- The lots the ids named when the row was written: those of a replay of
    -- every row appended before it, in @key form. NULL when the ledger cannot
    -- establish it.
    as_written      TEXT,
    -- The lots the ids named as the ledger was read before schema 10. NULL
    -- when the row did not apply.
    as_read         TEXT,
    -- The meaning in force, in @key form. NULL while pending: replay refuses
    -- the row until it is decided.
    lot_selection   TEXT,
    status          TEXT NOT NULL CHECK (status IN (
        'agreed',      -- both readings name the same lots
        'pending',     -- they differ, or one cannot be established
        'as_written',  -- the owner chose what the row meant when written
        'as_read'      -- the owner chose what it read as before schema 10
    )),
    -- Why a row is pending, or the owner's reason for deciding it.
    note            TEXT,
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    CHECK ((status = 'pending') = (lot_selection IS NULL)),
    CHECK (status <> 'agreed' OR (as_written IS NOT NULL
        AND as_written = as_read AND lot_selection = as_written)),
    CHECK (status <> 'as_written' OR (as_written IS NOT NULL
        AND lot_selection = as_written)),
    CHECK (status <> 'as_read' OR (as_read IS NOT NULL AND lot_selection = as_read)),
    CHECK (status NOT IN ('as_written', 'as_read')
        OR (decided_at IS NOT NULL AND note IS NOT NULL))
);

-- A pending row is decided once, and nothing else about a row changes. A
-- recorded meaning that could be rewritten would be a ledger that could be.
CREATE TRIGGER IF NOT EXISTS trg_lot_designation_decided_once
BEFORE UPDATE ON lot_designation
WHEN OLD.status <> 'pending'
    OR NEW.txn_id <> OLD.txn_id
    OR NEW.as_written IS NOT OLD.as_written
    OR NEW.as_read IS NOT OLD.as_read
    OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'PT-E-DESIGNATION-IMMUTABLE: a recorded designation is decided once and never changed');
END;

CREATE TRIGGER IF NOT EXISTS trg_lot_designation_no_delete
BEFORE DELETE ON lot_designation
BEGIN
    SELECT RAISE(ABORT, 'PT-E-DESIGNATION-IMMUTABLE: a recorded designation is decided once and never changed');
END;
