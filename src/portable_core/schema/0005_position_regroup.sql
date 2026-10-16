-- portable:rebuild
--
-- Position regrouping as a ledger fact, under the rebuild mechanism of
-- ADR 0019. ADR 0023 records the decision.
--
-- A position is the unit of trader intent (ADR 0009): a long stock holding
-- becomes a covered call when a call is written against it, and `pt position
-- group` says so. It used to say so by editing position_leg -- derived state,
-- which every rebuild discards. So a grouping vanished on the next back-dated
-- import, reversal or `pt rebuild`, and `pt validate` failed the moment one was
-- made. A grouping is now a row in the ledger, replayed like any other:
--
--   position_regroup -- move this account's leg in instrument_id into the
--   position opened by related_txn_id, or into a new position when that is
--   NULL; restate the strategy when strategy_type says so.
--
-- The target is named by the transaction that opened it, not by position_id:
-- that is a derived surrogate key, renumbered by any rebuild that replays in a
-- different order. `transaction.position_id`, which held one, is kept as
-- recorded on the rows that carry it and is not written again.
--
-- SQLite cannot alter a CHECK constraint, so "transaction" is REBUILT,
-- preserving every row and every column -- all 33 of them, named one by one
-- below, because 0004 shipped copying fewer than the table had (ADR 0022).

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
        -- fund capital-gain distributions, by character. Income for flow
        -- purposes (never external, PORT-GIPS-B02); taxed by character, which
        -- is why there are two and not one with a flag: a report that sums
        -- them into dividends is a wrong number in a tax year.
        'capital_gain_lt', 'capital_gain_st',
        -- corporate actions
        'split', 'reverse_split', 'stock_dividend', 'spinoff',
        'merger_cash', 'merger_stock', 'merger_mixed', 'symbol_change', 'delist',
        -- options lifecycle
        'option_exercise', 'option_assignment', 'option_expiration',
        -- fixed income lifecycle
        'bond_amortization', 'bond_accretion', 'bond_call', 'bond_maturity',
        -- adjustments
        'reversal', 'correction',
        -- trader intent, replayed (ADR 0023): no quantity, no cash
        'position_regroup'
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

    -- ADR 0023. The strategy a position_regroup restates for the position it
    -- moves a leg into. NULL there means "name it from its legs"; on any other
    -- row it is always NULL.
    strategy_type   TEXT CHECK (strategy_type IN (
        'single', 'covered_call', 'vertical', 'calendar', 'diagonal',
        'collar', 'straddle', 'strangle', 'custom'
    )),

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
    CHECK (txn_type NOT IN ('transfer_in', 'transfer_out') OR net_cash_effect = '0.00'),
    -- A regroup moves one leg -- this account's holding of instrument_id --
    -- and nothing else: no quantity changes hands and no cash moves, so a
    -- change of intent cannot change a lot, a basis or a balance (ADR 0009).
    CHECK (
        txn_type <> 'position_regroup'
        OR (instrument_id IS NOT NULL AND quantity IS NULL AND net_cash_effect = '0.00')
    ),
    CHECK (txn_type = 'position_regroup' OR strategy_type IS NULL)
);

INSERT INTO transaction_new (
    txn_id, account_id, trade_date, settlement_date, seq, txn_type,
    instrument_id, quantity, price, gross_amount, fees, commissions,
    taxes_withheld, withholding_reclaimable, fee_class, net_cash_effect,
    position_id, counter_account_id, related_txn_id, reverses_txn_id,
    lot_selection, relief_method, ex_date, pay_date, is_qualified, note,
    external_ref, source, original_basis, original_acquired_date,
    basis_source, basis_assumption, created_at
)
-- strategy_type is new and NULL on every existing row.
SELECT
    txn_id, account_id, trade_date, settlement_date, seq, txn_type,
    instrument_id, quantity, price, gross_amount, fees, commissions,
    taxes_withheld, withholding_reclaimable, fee_class, net_cash_effect,
    position_id, counter_account_id, related_txn_id, reverses_txn_id,
    lot_selection, relief_method, ex_date, pay_date, is_qualified, note,
    external_ref, source, original_basis, original_acquired_date,
    basis_source, basis_assumption, created_at
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
