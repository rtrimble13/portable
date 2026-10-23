-- Price history accepted against the custodian's statements. ADR 0031,
-- answering ADR 0020 R10.
--
-- Nothing checked that loaded prices were right. A custodian's statement says
-- what each position was worth on its date, which is an independent anchor:
-- a price from the wrong day, the wrong instrument or the adjusted series
-- disagrees with it. `pt reconcile --against ... --record` stores a
-- statement's priced lines here, and whether each one ties is derived on
-- demand from the anchor and the prices held at the time -- so reloading a
-- price re-decides it, and nothing goes stale.
--
-- Additive: two new tables. Neither is derived state; a rebuild leaves both
-- alone, and `pt export` carries both.

-- @table statement_position
-- What a custodian's statement says an account held of one instrument on a
-- date, and what it was worth: the anchor price history is accepted against
-- (ADR 0031). Reference data, like a price -- the ledger does not derive it.
-- Whether it ties is computed when asked, never stored.
CREATE TABLE IF NOT EXISTS statement_position (
    statement_position_id INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES account (account_id),
    -- The statement's date: the end of the day it values.
    statement_date  TEXT NOT NULL,
    instrument_id   INTEGER NOT NULL REFERENCES instrument (instrument_id),
    -- The custodian's quantity, which the price comparison uses so that a
    -- quantity break is not also reported as a price break.
    quantity        TEXT NOT NULL,  -- decimal
    -- The custodian's market value, as stated: the cent-rounded figure.
    market_value    TEXT NOT NULL,  -- decimal
    -- The statement it came from (a file name), per PORT-GIPS-J03.
    source          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (account_id, statement_date, instrument_id)
);

-- @table price_acceptance
-- A person's acceptance of one disagreement between a statement line and
-- portable's price, with the reason (ADR 0031). It applies only while the
-- figures it accepted are unchanged: the statement's quantity and market
-- value and portable's price and its date. A reloaded price or a re-recorded
-- statement makes the line a break again.
CREATE TABLE IF NOT EXISTS price_acceptance (
    price_acceptance_id INTEGER PRIMARY KEY,
    statement_position_id INTEGER NOT NULL
        REFERENCES statement_position (statement_position_id),
    -- What was accepted: the statement's figures...
    quantity        TEXT NOT NULL,  -- decimal
    market_value    TEXT NOT NULL,  -- decimal
    -- ...and the price portable held for that date, with the date of that
    -- price (the newest on or before the statement date).
    price           TEXT NOT NULL,  -- decimal
    price_date      TEXT NOT NULL,
    -- Why the difference is right, in the owner's words. Never empty.
    reason          TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_price_acceptance_position
    ON price_acceptance (statement_position_id);
