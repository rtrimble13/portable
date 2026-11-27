-- portable .bmk schema, version 1 (ADR 0050).
--
-- @schema_version 1
-- @name initial
--
-- A .bmk file is one index: its definition, the market data it was built
-- from, and the levels built from them. It follows the .port's conventions
-- (`src/portable_core/schema/0001_initial.sql`):
--
--   * Decimals are TEXT in canonical form, marked `-- decimal`. No REAL,
--     FLOAT, DOUBLE or NUMERIC anywhere (ADR 0005).
--   * Dates are TEXT `YYYY-MM-DD`; timestamps TEXT ISO-8601 UTC with `Z`,
--     read from the injected clock (ADR 0035).
--   * Booleans are INTEGER CHECK (x IN (0, 1)).
--   * Comments here generate docs/bmk-schema.md, as the .port's do.
--
-- Table classes, which drive `bmk build` as ADR 0010's drive `pt rebuild`:
--
--   DEFINITION  methodology, reconstitution*, security_event,
--               distribution_schedule. Entered by the owner, effective-dated,
--               never edited: a row entered wrongly is corrected on its own
--               date (ADR 0025).
--   REFERENCE   security*, trading_day, close, split, dividend, market_cap.
--               Facts about the world, each with its source and as-of. A
--               refresh adds rows; changing one already used is a restatement,
--               logged in input_restatement.
--   DERIVED     build, level, holding, rebalance_weight, reinvestment.
--               Dropped and rebuilt by `bmk build` from the two classes above.
--
-- An index holds UNITS of each constituent, not weights (ADR 0050 §3). On a
-- rebalance or reconstitution close the method's target weights are turned
-- into units; between them the units hold and the weights drift with prices.
-- The level on day t is
--
--     level_t = sum_i( units_i,t * close_i,t ) / divisor_t
--
-- For `custom`, `equal` and `cap` the units are scaled so that the divisor
-- stays 1 and the notional value is the level itself. For `price` every
-- constituent holds one unit and the divisor carries the continuity, as a
-- price-weighted index's does.
--
-- The total-return level reinvests each cash dividend across the whole index
-- at the ex-date close (ADR 0050, answer 4). That scales every unit by one
-- factor, which leaves the weights unchanged, so it is computed as a chain on
-- the price level rather than by storing a second set of units:
--
--     tr_t = tr_{t-1} * (price_level_t + dividend_points_t) / price_level_{t-1}
--
-- where dividend_points_t = sum_i( units_i * dividend_i,t ) / divisor_t over
-- the dividends going ex on t. Each one is a `reinvestment` row.

-- =============================================================================
-- META
-- =============================================================================

-- @table schema_migration
-- Applied migrations, one row each, as in the .port.
CREATE TABLE IF NOT EXISTS schema_migration (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    -- SHA-256 of the migration text as applied.
    checksum    TEXT NOT NULL,
    applied_at  TEXT NOT NULL,
    applied_by  TEXT NOT NULL
);

-- @table meta
-- The index's identity, as key/value. Required keys, validated by
-- `bmk validate`:
--   schema_version, index_name, description, base_date, base_level,
--   currency (ISO 4217; USD only in version 1), calendar (the exchange whose
--   trading days the index is computed on, e.g. XNYS), created_at,
--   portable_version.
-- `index_name` is what a report shows, so it is specific:
-- "US large-cap equal-weight, quarterly", not "Benchmark".
-- `base_date` and `base_level` never change once a level is built: moving
-- either rewrites every level.
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- =============================================================================
-- REFERENCE: the securities
-- =============================================================================

-- @table security
-- A security the index has held or may hold. Identity is fafnir's security
-- id, resolved through `duk` (ADR 0032), never a ticker: tickers are reused.
CREATE TABLE IF NOT EXISTS security (
    security_id     INTEGER PRIMARY KEY,
    -- fafnir's `sid`, the key every `duk` read uses. NULL only for a security
    -- fafnir does not carry, whose inputs then all come from files.
    fafnir_sid      INTEGER UNIQUE,
    -- The current ticker. NOT an identifier; see security_symbol_history.
    symbol          TEXT NOT NULL,
    name            TEXT,
    security_type   TEXT NOT NULL
        CHECK (security_type IN ('equity', 'etf', 'mutual_fund', 'adr', 'reit')),
    currency        TEXT NOT NULL DEFAULT 'USD',
    -- The issuer, so one issuer is held once (ADR 0050 §6: `duk mc` counts
    -- every class of a multi-class issuer under its primary class). The CIK
    -- where fafnir has one. `bmk build` refuses a membership holding two
    -- securities of one issuer.
    issuer_key      TEXT,
    source          TEXT NOT NULL,
    provider_ref    TEXT,
    created_at      TEXT NOT NULL
);

-- @table security_symbol_history
-- Ticker history from `duk`, so a symbol in a definition resolves to the
-- security that traded under it on that date.
CREATE TABLE IF NOT EXISTS security_symbol_history (
    security_id     INTEGER NOT NULL REFERENCES security (security_id),
    symbol          TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    -- NULL: still valid.
    valid_to        TEXT,
    source          TEXT NOT NULL,
    PRIMARY KEY (security_id, symbol, valid_from)
);

CREATE INDEX IF NOT EXISTS ix_bmk_symbol_lookup
    ON security_symbol_history (symbol, valid_from);

-- =============================================================================
-- DEFINITION
-- =============================================================================

-- @table methodology
-- How the index weights and rebalances, from a date. The first takes effect
-- on `base_date`. A later one is a methodology change, applied at the close
-- of its date and disclosed with it (PORT-GIPS-G04 once assigned).
CREATE TABLE IF NOT EXISTS methodology (
    methodology_id  INTEGER PRIMARY KEY,
    -- Applied at this date's close.
    effective_from  TEXT NOT NULL,
    -- ADR 0050 §3:
    --   custom  the weights each reconstitution declares
    --   equal   1/N of the members
    --   cap     each member's market cap x float factor, over the total
    --   price   one unit of each member, and a divisor
    weighting       TEXT NOT NULL CHECK (weighting IN ('custom', 'equal', 'cap', 'price')),
    -- When the units are reset to the targets, at the close of the period's
    -- last trading day (answer 6). `drift_band` resets at a close where any
    -- weight stands further from its target than `drift_band`. `never` holds
    -- the units from each reconstitution until the next.
    rebalance_rule  TEXT NOT NULL
        CHECK (rebalance_rule IN (
            'daily', 'monthly', 'quarterly', 'semiannual', 'annual', 'drift_band', 'never'
        )),
    -- Absolute, in weight units: 0.05 is five percentage points.
    drift_band      TEXT,  -- decimal
    note            TEXT,
    created_at      TEXT NOT NULL,
    -- The methodology of the same date this corrects (ADR 0025).
    corrects_methodology_id INTEGER REFERENCES methodology (methodology_id),
    CHECK ((rebalance_rule = 'drift_band') = (drift_band IS NOT NULL)),
    -- A drift band needs targets that hold still between rebalances; a cap
    -- target moves every day, and a price-weighted index has none.
    CHECK (rebalance_rule <> 'drift_band' OR weighting IN ('custom', 'equal')),
    -- A price-weighted index holds one unit of each member. There is nothing
    -- to rebalance to; only a reconstitution changes it.
    CHECK (weighting <> 'price' OR rebalance_rule = 'never')
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_methodology_original
    ON methodology (effective_from) WHERE corrects_methodology_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_methodology_corrects
    ON methodology (corrects_methodology_id) WHERE corrects_methodology_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_methodology_correction_same_key
BEFORE INSERT ON methodology
WHEN NEW.corrects_methodology_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM methodology o
    WHERE o.methodology_id = NEW.corrects_methodology_id
      AND o.effective_from = NEW.effective_from
)
BEGIN
    SELECT RAISE(ABORT, 'a correction names a methodology of the same date');
END;

-- @table reconstitution
-- The full membership from a date's close: every member, not a change list,
-- so any date's membership is read from one row. The first is on
-- `base_date`. Distinct from a rebalance, which resets weights among the
-- same members by rule (ADR 0050, trap 5).
CREATE TABLE IF NOT EXISTS reconstitution (
    reconstitution_id INTEGER PRIMARY KEY,
    -- Applied at this date's close.
    effective_date  TEXT NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL,
    corrects_reconstitution_id INTEGER REFERENCES reconstitution (reconstitution_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_reconstitution_original
    ON reconstitution (effective_date) WHERE corrects_reconstitution_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_reconstitution_corrects
    ON reconstitution (corrects_reconstitution_id)
    WHERE corrects_reconstitution_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_reconstitution_correction_same_key
BEFORE INSERT ON reconstitution
WHEN NEW.corrects_reconstitution_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM reconstitution o
    WHERE o.reconstitution_id = NEW.corrects_reconstitution_id
      AND o.effective_date = NEW.effective_date
)
BEGIN
    SELECT RAISE(ABORT, 'a correction names a reconstitution of the same date');
END;

-- @table reconstitution_member
-- One member of a reconstitution.
CREATE TABLE IF NOT EXISTS reconstitution_member (
    reconstitution_id INTEGER NOT NULL REFERENCES reconstitution (reconstitution_id),
    security_id     INTEGER NOT NULL REFERENCES security (security_id),
    -- The target weight under `custom`: more than 0, at most 1, the members'
    -- summing to exactly 1 (checked in Decimal by the service, as ADR 0048's
    -- are). NULL under every other method.
    weight          TEXT,  -- decimal
    -- Under `cap`: the share of the count that floats, more than 0, at most
    -- 1. NULL is full shares, which is what fafnir carries (no float in
    -- SF1). Stated with its source when given.
    float_factor    TEXT,  -- decimal
    float_source    TEXT,
    PRIMARY KEY (reconstitution_id, security_id),
    CHECK ((float_factor IS NULL) = (float_source IS NULL))
);

-- @table security_event
-- What happened to a member that fafnir does not carry: a delisting, a
-- merger or a spinoff (ADR 0050 §5). Declared, never assumed. A member that
-- stops trading with no row here stops the build.
CREATE TABLE IF NOT EXISTS security_event (
    event_id        INTEGER PRIMARY KEY,
    security_id     INTEGER NOT NULL REFERENCES security (security_id),
    -- The last day the member is held, at its close.
    event_date      TEXT NOT NULL,
    event_type      TEXT NOT NULL
        CHECK (event_type IN ('delisting', 'cash_merger', 'stock_merger', 'spinoff')),
    -- delisting, cash_merger: the value per share received, reinvested
    -- across the index at the close. A delisting at zero states zero.
    cash_per_share  TEXT,  -- decimal
    -- stock_merger, spinoff: the security received, and how many of it per
    -- share held. A spinoff keeps the parent; the child is held until the
    -- next reconstitution or rebalance unless it is a member.
    received_security_id INTEGER REFERENCES security (security_id),
    received_ratio  TEXT,  -- decimal
    source          TEXT NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL,
    corrects_event_id INTEGER REFERENCES security_event (event_id),
    CHECK (
        (event_type IN ('delisting', 'cash_merger')
            AND cash_per_share IS NOT NULL AND received_security_id IS NULL)
        OR (event_type IN ('stock_merger', 'spinoff')
            AND received_security_id IS NOT NULL AND received_ratio IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_security_event_original
    ON security_event (security_id, event_date, event_type)
    WHERE corrects_event_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_security_event_corrects
    ON security_event (corrects_event_id) WHERE corrects_event_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_security_event_correction_same_key
BEFORE INSERT ON security_event
WHEN NEW.corrects_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM security_event o
    WHERE o.event_id = NEW.corrects_event_id
      AND o.security_id = NEW.security_id
      AND o.event_date = NEW.event_date
)
BEGIN
    SELECT RAISE(ABORT, 'a correction names an event of the same security and date');
END;

-- @table distribution_schedule
-- How often a security pays, from a date (ADR 0050, answer 3). A security
-- that declares a schedule and has a longer gap between dividends in the
-- source stops the build, naming the gap: a missing dividend would
-- understate the total return silently. `none` declares a non-payer, so a
-- dividend that does arrive is also a stop: the declaration was wrong.
CREATE TABLE IF NOT EXISTS distribution_schedule (
    schedule_id     INTEGER PRIMARY KEY,
    security_id     INTEGER NOT NULL REFERENCES security (security_id),
    effective_from  TEXT NOT NULL,
    schedule        TEXT NOT NULL
        CHECK (schedule IN ('none', 'monthly', 'quarterly', 'semiannual', 'annual', 'irregular')),
    -- `irregular` (a special-dividend payer, a fund paying when it realises
    -- gains) states the longest gap the source may show before the build
    -- stops, in days.
    max_gap_days    INTEGER CHECK (max_gap_days > 0),
    source          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    corrects_schedule_id INTEGER REFERENCES distribution_schedule (schedule_id),
    CHECK ((schedule = 'irregular') = (max_gap_days IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_distribution_schedule_original
    ON distribution_schedule (security_id, effective_from)
    WHERE corrects_schedule_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_distribution_schedule_corrects
    ON distribution_schedule (corrects_schedule_id) WHERE corrects_schedule_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_distribution_schedule_correction_same_key
BEFORE INSERT ON distribution_schedule
WHEN NEW.corrects_schedule_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM distribution_schedule o
    WHERE o.schedule_id = NEW.corrects_schedule_id
      AND o.security_id = NEW.security_id
      AND o.effective_from = NEW.effective_from
)
BEGIN
    SELECT RAISE(ABORT, 'a correction names a schedule of the same security and date');
END;

-- =============================================================================
-- REFERENCE: the market data
-- =============================================================================

-- @table trading_day
-- The calendar the index is computed on: every trading day of `meta.calendar`
-- from `base_date`, from `duk`. A member with no close on one of these days
-- stops the build (ADR 0050 §5). There is no carried price here.
CREATE TABLE IF NOT EXISTS trading_day (
    trade_date      TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    as_of           TEXT NOT NULL
);

-- @table close
-- Unadjusted closes (`duk ph --close --exact`), or from a file where fafnir
-- has none. Adjusted prices are never stored: the total return is built from
-- these and the actions below, so every payment is visible.
CREATE TABLE IF NOT EXISTS close (
    security_id     INTEGER NOT NULL REFERENCES security (security_id),
    trade_date      TEXT NOT NULL,
    close           TEXT NOT NULL,  -- decimal
    currency        TEXT NOT NULL DEFAULT 'USD',
    -- 'fafnir:duk ph', 'file:<name>'.
    source          TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    provider_ref    TEXT,
    PRIMARY KEY (security_id, trade_date)
);

CREATE INDEX IF NOT EXISTS ix_close_date ON close (trade_date);

-- @table split
-- Splits from `duk ca`. 2-for-1 is numerator 2, denominator 1. On the
-- ex-date a `custom`, `equal` or `cap` index multiplies the member's units
-- by the ratio; a `price` index keeps one unit and changes its divisor.
-- Neither level moves.
CREATE TABLE IF NOT EXISTS split (
    security_id     INTEGER NOT NULL REFERENCES security (security_id),
    ex_date         TEXT NOT NULL,
    numerator       TEXT NOT NULL,  -- decimal
    denominator     TEXT NOT NULL,  -- decimal
    source          TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    provider_ref    TEXT,
    PRIMARY KEY (security_id, ex_date)
);

-- @table dividend
-- Cash dividends from `duk ca`, gross of withholding (so the index is not
-- net of withholding, PORT-GIPS-G05). Entitlement is on the ex-date, which
-- is when the total-return level reinvests it.
CREATE TABLE IF NOT EXISTS dividend (
    dividend_id     INTEGER PRIMARY KEY,
    security_id     INTEGER NOT NULL REFERENCES security (security_id),
    ex_date         TEXT NOT NULL,
    pay_date        TEXT,
    amount          TEXT NOT NULL,  -- decimal, per share, in the security's currency
    currency        TEXT NOT NULL DEFAULT 'USD',
    source          TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    provider_ref    TEXT,
    UNIQUE (security_id, ex_date, amount)
);

-- @table market_cap
-- Each member's market cap on each `cap` rebalance and reconstitution close,
-- from `duk mc --exact` (ADR 0050 §6), with what it was computed from so a
-- weight traces to its filing. Point in time: the count is the latest
-- balance sheet known on `cap_date`. A file may supply one where `duk mc`
-- has none (an ETF, a security Sharadar does not feed); its source says so.
CREATE TABLE IF NOT EXISTS market_cap (
    security_id     INTEGER NOT NULL REFERENCES security (security_id),
    cap_date        TEXT NOT NULL,
    market_cap      TEXT NOT NULL,  -- decimal, in `currency`
    -- What `duk mc` multiplied: close_raw x shares_outstanding x share_factor.
    -- NULL for a cap a file supplied whole.
    close_raw       TEXT,  -- decimal
    shares_outstanding TEXT,  -- decimal
    share_factor    TEXT,  -- decimal
    -- The balance sheet the count came from.
    fiscal_date     TEXT,
    known_from      TEXT,
    currency        TEXT NOT NULL DEFAULT 'USD',
    source          TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    PRIMARY KEY (security_id, cap_date),
    CHECK (
        (close_raw IS NULL) = (shares_outstanding IS NULL)
        AND (close_raw IS NULL) = (share_factor IS NULL)
    )
);

-- @table input_restatement
-- A reference row changed after a level was built from it: a vendor
-- correction, a dividend fafnir added late. `bmk refresh` refuses such a
-- change unless `--restate` is given, and then records it here, so a level
-- that moved can be traced to why. Append-only.
CREATE TABLE IF NOT EXISTS input_restatement (
    restatement_id  INTEGER PRIMARY KEY,
    table_name      TEXT NOT NULL
        CHECK (table_name IN ('close', 'split', 'dividend', 'market_cap', 'trading_day')),
    -- The row's key, e.g. 'security_id=4;trade_date=2024-03-28'.
    row_key         TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- =============================================================================
-- DERIVED: dropped and rebuilt by `bmk build`
-- =============================================================================

-- @table build
-- The build the derived tables came from. One row: a build replaces them all.
CREATE TABLE IF NOT EXISTS build (
    build_id        INTEGER PRIMARY KEY CHECK (build_id = 1),
    -- SHA-256 over the DEFINITION and REFERENCE rows in canonical order. Two
    -- files with the same hash build the same levels; `pt benchmark assign`
    -- records it as the series' source (ADR 0050, answer 8).
    inputs_hash     TEXT NOT NULL,
    first_date      TEXT NOT NULL,
    last_date       TEXT NOT NULL,
    built_at        TEXT NOT NULL,
    built_by        TEXT NOT NULL
);

-- @table level
-- One row per trading day from `base_date` to the build's last date.
CREATE TABLE IF NOT EXISTS level (
    level_date      TEXT PRIMARY KEY,
    -- From unadjusted closes and units. `price_only`: never assignable as a
    -- benchmark (PORT-GIPS-G01), kept for factor work and as the base of the
    -- total-return chain.
    price_level     TEXT NOT NULL,  -- decimal
    -- Dividends reinvested across the index at the ex-date close. The level
    -- a benchmark is assigned from.
    total_return_level TEXT NOT NULL,  -- decimal
    -- Index points of the dividends going ex this day. Zero, not NULL, on a
    -- day with none: it is a sum, and it is known.
    dividend_points TEXT NOT NULL,  -- decimal
    -- 1 for custom, equal and cap.
    divisor         TEXT NOT NULL,  -- decimal
    -- What happened at this close, so a level's change can be read.
    is_rebalance    INTEGER NOT NULL CHECK (is_rebalance IN (0, 1)),
    is_reconstitution INTEGER NOT NULL CHECK (is_reconstitution IN (0, 1)),
    member_count    INTEGER NOT NULL CHECK (member_count > 0)
);

-- @table holding
-- The units of each member over a span of days in which they did not
-- change. Units change only at a rebalance, a reconstitution, a split or a
-- security event, so spans, not one row per member per day.
CREATE TABLE IF NOT EXISTS holding (
    security_id     INTEGER NOT NULL REFERENCES security (security_id),
    -- First and last day held at these units, inclusive.
    from_date       TEXT NOT NULL,
    to_date         TEXT NOT NULL,
    units           TEXT NOT NULL,  -- decimal
    PRIMARY KEY (security_id, from_date),
    CHECK (from_date <= to_date)
);

-- @table rebalance_weight
-- At each rebalance and reconstitution close: each member's target weight,
-- the price and units it was set at, and for `cap` the cap it came from. The
-- composition a report discloses as of each annual period end (PORT-GIPS-G03)
-- is read from here and `holding`.
CREATE TABLE IF NOT EXISTS rebalance_weight (
    rebalance_date  TEXT NOT NULL,
    security_id     INTEGER NOT NULL REFERENCES security (security_id),
    target_weight   TEXT NOT NULL,  -- decimal
    close           TEXT NOT NULL,  -- decimal
    units           TEXT NOT NULL,  -- decimal
    -- `cap` only: the market cap x float factor the weight came from.
    weighting_cap   TEXT,  -- decimal
    PRIMARY KEY (rebalance_date, security_id)
);

-- @table reinvestment
-- Every dividend the total-return level reinvested, and what it added (ADR
-- 0050, answer 3: each payment can be audited). The day's rows sum to
-- `level.dividend_points`.
CREATE TABLE IF NOT EXISTS reinvestment (
    dividend_id     INTEGER PRIMARY KEY REFERENCES dividend (dividend_id),
    level_date      TEXT NOT NULL,
    security_id     INTEGER NOT NULL REFERENCES security (security_id),
    units           TEXT NOT NULL,  -- decimal
    amount          TEXT NOT NULL,  -- decimal, per share
    index_points    TEXT NOT NULL   -- decimal
);

CREATE INDEX IF NOT EXISTS ix_reinvestment_date ON reinvestment (level_date);

-- =============================================================================
-- Never edited
-- =============================================================================
-- A definition row is corrected on its own date, and an input is restated
-- with a logged reason; neither is changed in place (ADR 0010, ADR 0025).

CREATE TRIGGER IF NOT EXISTS trg_methodology_no_update
BEFORE UPDATE ON methodology
BEGIN SELECT RAISE(ABORT, 'a methodology is not edited: correct it on its date'); END;
CREATE TRIGGER IF NOT EXISTS trg_methodology_no_delete
BEFORE DELETE ON methodology
BEGIN SELECT RAISE(ABORT, 'a methodology is not edited: correct it on its date'); END;

CREATE TRIGGER IF NOT EXISTS trg_reconstitution_no_update
BEFORE UPDATE ON reconstitution
BEGIN SELECT RAISE(ABORT, 'a reconstitution is not edited: correct it on its date'); END;
CREATE TRIGGER IF NOT EXISTS trg_reconstitution_no_delete
BEFORE DELETE ON reconstitution
BEGIN SELECT RAISE(ABORT, 'a reconstitution is not edited: correct it on its date'); END;

CREATE TRIGGER IF NOT EXISTS trg_reconstitution_member_no_update
BEFORE UPDATE ON reconstitution_member
BEGIN SELECT RAISE(ABORT, 'a reconstitution is not edited: correct it on its date'); END;
CREATE TRIGGER IF NOT EXISTS trg_reconstitution_member_no_delete
BEFORE DELETE ON reconstitution_member
BEGIN SELECT RAISE(ABORT, 'a reconstitution is not edited: correct it on its date'); END;

CREATE TRIGGER IF NOT EXISTS trg_security_event_no_update
BEFORE UPDATE ON security_event
BEGIN SELECT RAISE(ABORT, 'a security event is not edited: correct it on its date'); END;
CREATE TRIGGER IF NOT EXISTS trg_security_event_no_delete
BEFORE DELETE ON security_event
BEGIN SELECT RAISE(ABORT, 'a security event is not edited: correct it on its date'); END;

CREATE TRIGGER IF NOT EXISTS trg_distribution_schedule_no_update
BEFORE UPDATE ON distribution_schedule
BEGIN SELECT RAISE(ABORT, 'a distribution schedule is not edited: correct it on its date'); END;
CREATE TRIGGER IF NOT EXISTS trg_distribution_schedule_no_delete
BEFORE DELETE ON distribution_schedule
BEGIN SELECT RAISE(ABORT, 'a distribution schedule is not edited: correct it on its date'); END;

CREATE TRIGGER IF NOT EXISTS trg_input_restatement_no_update
BEFORE UPDATE ON input_restatement
BEGIN SELECT RAISE(ABORT, 'a restatement is a record, never edited'); END;
CREATE TRIGGER IF NOT EXISTS trg_input_restatement_no_delete
BEFORE DELETE ON input_restatement
BEGIN SELECT RAISE(ABORT, 'a restatement is a record, never edited'); END;
