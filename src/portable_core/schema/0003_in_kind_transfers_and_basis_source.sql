-- portable:rebuild
--
-- ADR 0015 (in-kind transfers) and ADR 0017 (basis provenance), together,
-- under the rebuild mechanism of ADR 0019.
--
-- One migration and not two: a `transfer_in` row is the thing that seeds a lot
-- whose basis is not `derived`, so shipping the transaction type without the
-- provenance column would mean a released version in which a seeded lot cannot
-- say where its basis came from -- the exact failure ADR 0017 exists to
-- prevent.
--
-- Two halves, and they are not equally risky:
--
--   "transaction" is REBUILT, preserving every row. SQLite cannot alter a
--   CHECK constraint, and `txn_type` needs two new members, so the twelve-step
--   procedure is the only route. This is the delicate half: the ledger is the
--   one table in the file that cannot be reconstructed from anything else.
--
--   `lot` is DROPPED and recreated empty. Lots are derived state (CLAUDE.md
--   invariant 3), so nothing is copied; `pt rebuild` regenerates them. A
--   NOT NULL column with no default cannot be added to an existing table in
--   SQLite at all -- and inventing a default would defeat the column, whose
--   whole purpose is that a writer cannot create a lot without answering the
--   question.
--
-- See 0002_external_ref_unique.sql for the note on why migrations are never
-- edited once applied.

-- ── Derived state first, so the lot rebuild has nothing to preserve ─────────
-- The same list, in the same order, as Repositories.clear_derived. A file is
-- left with no derived state and `pt validate` says so until `pt rebuild`
-- runs: a reported, recoverable state rather than a silent one (invariant 9).
DELETE FROM snapshot_flow;
DELETE FROM valuation_snapshot_price;
DELETE FROM valuation_snapshot;
DELETE FROM realized_gain;
DELETE FROM lot_disposition;
DELETE FROM lot_basis_adjustment;
DELETE FROM lot;
DELETE FROM position_leg;
DELETE FROM position;
DELETE FROM cash_balance;

-- ── The ledger ──────────────────────────────────────────────────────────────
-- Triggers are dropped explicitly rather than left to fall with the table, so
-- that recreating them below is a visible, reviewable pair. The runner
-- compares the trigger inventory across the rebuild and refuses a migration
-- that does not restore one (ADR 0019 §1): a file that lost
-- trg_transaction_no_update would look entirely fine and no longer be
-- append-only.
DROP TRIGGER IF EXISTS trg_transaction_no_update;
DROP TRIGGER IF EXISTS trg_transaction_no_delete;

CREATE TABLE transaction_new (
    txn_id          INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES account (account_id),
    trade_date      TEXT NOT NULL,
    settlement_date TEXT,
    seq             INTEGER NOT NULL,
    txn_type        TEXT NOT NULL CHECK (txn_type IN (
        -- trades
        'buy', 'sell', 'sell_short', 'buy_to_cover',
        -- cash
        'deposit', 'withdrawal', 'transfer', 'journal',
        'interest', 'fee', 'margin_interest',
        -- securities crossing the portfolio boundary without being traded
        -- (ADR 0015). NOT 'transfer', which means an internal movement that
        -- nets to zero at portfolio level; these cross the boundary and do not
        -- net, so overloading one type would put two opposite flow
        -- classifications behind it.
        'transfer_in', 'transfer_out',
        -- income
        'dividend', 'dividend_reinvest', 'return_of_capital', 'coupon',
        'accrual_income',
        -- corporate actions
        'split', 'reverse_split', 'stock_dividend', 'spinoff',
        'merger_cash', 'merger_stock', 'merger_mixed', 'symbol_change', 'delist',
        -- options lifecycle
        'option_exercise', 'option_assignment', 'option_expiration',
        -- fixed income lifecycle
        'bond_amortization', 'bond_accretion', 'bond_call', 'bond_maturity',
        -- adjustments
        'reversal', 'correction'
    )),
    instrument_id   INTEGER REFERENCES instrument (instrument_id),
    quantity        TEXT,  -- decimal
    price           TEXT,  -- decimal
    gross_amount    TEXT,  -- decimal
    fees            TEXT NOT NULL DEFAULT '0.00',  -- decimal
    commissions     TEXT NOT NULL DEFAULT '0.00',  -- decimal
    taxes_withheld  TEXT NOT NULL DEFAULT '0.00',  -- decimal
    withholding_reclaimable TEXT,  -- decimal
    fee_class       TEXT CHECK (fee_class IN (
        'transaction_cost', 'embedded_fund_fee', 'external_mgmt_fee',
        'internal_mgmt_cost', 'other_admin'
    )),
    net_cash_effect TEXT NOT NULL,  -- decimal
    position_id     INTEGER,
    counter_account_id INTEGER REFERENCES account (account_id),
    related_txn_id  INTEGER REFERENCES "transaction" (txn_id),
    reverses_txn_id INTEGER REFERENCES "transaction" (txn_id),
    lot_selection   TEXT,
    relief_method   TEXT CHECK (relief_method IN
        ('spec', 'fifo', 'lifo', 'hifo', 'lofo', 'avg')),
    ex_date         TEXT,
    pay_date        TEXT,
    is_qualified    INTEGER CHECK (is_qualified IN (0, 1)),
    note            TEXT,
    external_ref    TEXT,
    source          TEXT NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'import', 'derived')),

    -- ADR 0015. A transfer_in carries TWO numbers that must not be conflated:
    -- the market value on the transfer date, which is the flow amount and
    -- lives in price/gross_amount as it does for any other row; and the
    -- delivering custodian's basis and acquisition date, which are unrelated
    -- to the transfer and are what the tax engine uses forever after. Use
    -- market value as basis and every future sale reports the wrong gain; use
    -- basis as the flow amount and the period's return is wrong by the whole
    -- unrealized gain.
    original_basis  TEXT,  -- decimal
    original_acquired_date TEXT,

    -- ADR 0017. Where the basis came from. Stored on the LEDGER row and not
    -- only on the lot because the difference between 'reconstructed',
    -- 'estimated' and 'unavailable' is an assertion by whoever built the lot,
    -- not a consequence of its numbers -- and invariant 3 requires derived
    -- state to be reproducible by replaying the ledger. The replay copies
    -- these onto the lot it opens; every other opening transaction produces
    -- 'derived'.
    basis_source    TEXT CHECK (basis_source IN (
        'derived', 'reconstructed', 'estimated', 'unavailable',
        'custodian_asserted'
    )),
    basis_assumption TEXT,

    created_at      TEXT NOT NULL,
    UNIQUE (trade_date, seq),
    CHECK (
        (fees = '0.00' AND commissions = '0.00')
        OR fee_class IS NOT NULL
    ),
    CHECK (
        (txn_type = 'transfer' AND counter_account_id IS NOT NULL)
        OR (txn_type <> 'transfer' AND counter_account_id IS NULL)
    ),
    CHECK ((txn_type = 'reversal') = (reverses_txn_id IS NOT NULL)),

    -- The in-kind columns belong to the in-kind types and to nothing else. A
    -- basis and an acquisition date on a `buy` would be read later as if they
    -- meant something, and they would disagree with the row they sit on.
    CHECK (
        txn_type IN ('transfer_in', 'transfer_out')
        OR (original_basis IS NULL
            AND original_acquired_date IS NULL
            AND basis_assumption IS NULL)
    ),
    -- A transfer_in creates a lot, so it must say where the basis came from.
    -- NULL here would be the same landmine `lot.basis_source NOT NULL` exists
    -- to remove, one table upstream.
    CHECK (txn_type <> 'transfer_in' OR basis_source IS NOT NULL),
    -- 'unavailable' is the one rung that legitimately carries no figure: no
    -- equation constrains the block, so a number would be invented. Every
    -- other rung asserts one.
    CHECK (
        txn_type <> 'transfer_in'
        OR basis_source = 'unavailable'
        OR original_basis IS NOT NULL
    ),
    -- Nothing moved but securities (ADR 0015). Stated in the schema so that a
    -- future writer cannot record an in-kind transfer that also moves cash,
    -- which would be two events wearing one row.
    CHECK (txn_type NOT IN ('transfer_in', 'transfer_out') OR net_cash_effect = '0.00')
);

INSERT INTO transaction_new (
    txn_id, account_id, trade_date, settlement_date, seq, txn_type,
    instrument_id, quantity, price, gross_amount, fees, commissions,
    taxes_withheld, withholding_reclaimable, fee_class, net_cash_effect,
    position_id, counter_account_id, related_txn_id, reverses_txn_id,
    lot_selection, relief_method, ex_date, pay_date, is_qualified, note,
    external_ref, source, created_at
)
SELECT
    txn_id, account_id, trade_date, settlement_date, seq, txn_type,
    instrument_id, quantity, price, gross_amount, fees, commissions,
    taxes_withheld, withholding_reclaimable, fee_class, net_cash_effect,
    position_id, counter_account_id, related_txn_id, reverses_txn_id,
    lot_selection, relief_method, ex_date, pay_date, is_qualified, note,
    external_ref, source, created_at
FROM "transaction";

DROP TABLE "transaction";

ALTER TABLE transaction_new RENAME TO "transaction";

CREATE INDEX IF NOT EXISTS ix_txn_account_date ON "transaction" (account_id, trade_date);
CREATE INDEX IF NOT EXISTS ix_txn_instrument_date ON "transaction" (instrument_id, trade_date);
CREATE INDEX IF NOT EXISTS ix_txn_position ON "transaction" (position_id);
CREATE INDEX IF NOT EXISTS ix_txn_type_date ON "transaction" (txn_type, trade_date);
CREATE INDEX IF NOT EXISTS ix_txn_order ON "transaction" (trade_date, seq);

-- Migration 0002's index, recreated: a rebuild drops it with the table, and
-- losing it would silently restore the duplicate-reference bug that migration
-- exists to prevent.
CREATE UNIQUE INDEX IF NOT EXISTS ux_txn_account_external_ref
    ON "transaction" (account_id, external_ref) WHERE external_ref IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_transaction_no_update
BEFORE UPDATE ON "transaction"
BEGIN
    SELECT RAISE(ABORT, 'PT-E-LEDGER-IMMUTABLE: the ledger is append-only; correct with a reversing entry (pt trade reverse), never by editing history');
END;

CREATE TRIGGER IF NOT EXISTS trg_transaction_no_delete
BEFORE DELETE ON "transaction"
BEGIN
    SELECT RAISE(ABORT, 'PT-E-LEDGER-IMMUTABLE: the ledger is append-only; correct with a reversing entry (pt trade reverse), never by deleting history');
END;

-- ── The lot ─────────────────────────────────────────────────────────────────
-- Recreated rather than altered: SQLite will not add a NOT NULL column with no
-- default to an existing table, and no default is the point. ADR 0017 §2: "no
-- writer can add a lot without answering the question".
DROP TABLE lot;

CREATE TABLE lot (
    lot_id          INTEGER PRIMARY KEY,
    leg_id          INTEGER NOT NULL REFERENCES position_leg (leg_id),
    position_id     INTEGER NOT NULL REFERENCES position (position_id),
    instrument_id   INTEGER NOT NULL REFERENCES instrument (instrument_id),
    account_id      INTEGER NOT NULL REFERENCES account (account_id),
    open_date       TEXT NOT NULL,
    open_txn_id     INTEGER NOT NULL REFERENCES "transaction" (txn_id),
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

    -- ADR 0017 §2. NOT NULL, no default. Every report that consumes a lot has
    -- to be able to say which of these it rested on, and a default would let a
    -- writer skip the question -- which is how an approximation gets mistaken
    -- for an exact figure, the one failure this ladder exists to prevent.
    basis_source    TEXT NOT NULL CHECK (basis_source IN (
        'derived',            -- computed by portable from its own ledger; exact
        'reconstructed',      -- custodian's present basis less later additions
        'estimated',          -- solved backwards under an assumed relief method
        'unavailable',        -- nothing survives to anchor a solve; NOT a claim
        'custodian_asserted'  -- read from a lot-detail report
    )),
    -- The arithmetic that produced a non-derived basis, in words, so it can be
    -- re-derived and re-argued later rather than merely trusted.
    basis_assumption TEXT,
    -- ADR 0017 §2: "`reconstructed`, `estimated`, and `unavailable` lots
    -- additionally carry the assumption that produced them". Those three are
    -- the approximate rungs, and an approximation with no stated reasoning is
    -- indistinguishable from a number somebody made up.
    --
    -- Not required of `custodian_asserted`, which is a figure somebody else
    -- stated rather than one portable worked out -- a provenance note there is
    -- useful and optional. Forbidden on `derived`, which is portable's own
    -- arithmetic and has no assumption to record.
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
