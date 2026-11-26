-- Asset classes, the classification of each instrument into them, and the
-- policy allocation drift is judged against. ADR 0048, accepted by the owner
-- on 2026-11-25.
--
-- Nothing derived reads these tables, and replay does not: they are
-- reference rows, read by `pt allocation drift` and `pt holdings --by
-- asset-class` on the date asked. Classifications and policies are
-- effective-dated and never edited, as tax rates and the return policy are
-- (ADR 0010). A row entered wrongly is corrected on its own date by a row
-- that names it (ADR 0025 decision 5), so each date's rows form one chain:
-- one original, each row corrected at most once.
--
-- A classification's weights, and a policy's targets, sum to exactly 1. That
-- is checked in Decimal by the service that writes them, and again where they
-- are read: SQLite could only add them up as floating point (CLAUDE.md
-- invariant 1).
--
-- Seeded from `instrument.asset_class`, the free text the owner typed: each
-- distinct value becomes a class, and each instrument that has one is
-- classified into it at weight 1 from the portfolio's inception (ADR 0048,
-- answer 5). The column is kept, and nothing writes it again. Every row
-- written here is stamped `:applied_at`, the migration runner's clock
-- (ADR 0035).
--
-- Additive: new tables only, no rebuild.

-- @table asset_class
-- The owner's asset classes. `cash` is built in, holds each account's cash
-- less its margin loan and every accrued dividend, and cannot be removed or
-- renamed. A class is never deleted while anything names it, and its name
-- never changes: past drift reports name it.
CREATE TABLE IF NOT EXISTS asset_class (
    class_id    INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE CHECK (length(name) > 0 AND name = trim(name)),
    description TEXT NOT NULL,
    -- 1 for `cash` only.
    is_builtin  INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0, 1)),
    created_at  TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_asset_class_name_is_fixed
BEFORE UPDATE OF name, is_builtin ON asset_class
BEGIN
    SELECT RAISE(ABORT, 'an asset class keeps its name: past drift reports name it');
END;

CREATE TRIGGER IF NOT EXISTS trg_asset_class_cash_stays
BEFORE DELETE ON asset_class
WHEN OLD.is_builtin = 1
BEGIN
    SELECT RAISE(ABORT, 'cash is a built-in asset class and cannot be removed');
END;

-- @table classification
-- Which class or classes an instrument belongs to, from a date. The parts are
-- in `classification_part`. A balanced fund is split by stated weights
-- (ADR 0048, answer 3).
CREATE TABLE IF NOT EXISTS classification (
    classification_id INTEGER PRIMARY KEY,
    instrument_id     INTEGER NOT NULL REFERENCES instrument (instrument_id),
    effective_from    TEXT NOT NULL,
    note              TEXT,
    created_at        TEXT NOT NULL,
    -- The classification of the same instrument and date this one corrects
    -- (ADR 0025). NULL for an original. A corrected row is kept and not read.
    corrects_classification_id INTEGER REFERENCES classification (classification_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_classification_original
    ON classification (instrument_id, effective_from)
    WHERE corrects_classification_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_classification_corrects
    ON classification (corrects_classification_id)
    WHERE corrects_classification_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_classification_correction_same_key
BEFORE INSERT ON classification
WHEN NEW.corrects_classification_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM classification o
    WHERE o.classification_id = NEW.corrects_classification_id
      AND o.instrument_id = NEW.instrument_id
      AND o.effective_from = NEW.effective_from
)
BEGIN
    SELECT RAISE(ABORT, 'a correction names a classification of the same instrument and date');
END;

-- @table classification_part
-- One class of a classification, and its share of the instrument's value.
CREATE TABLE IF NOT EXISTS classification_part (
    classification_id INTEGER NOT NULL REFERENCES classification (classification_id),
    class_id          INTEGER NOT NULL REFERENCES asset_class (class_id),
    -- The share of the instrument's value in this class: more than 0, at most
    -- 1. The parts of one classification sum to exactly 1.
    weight            TEXT NOT NULL,  -- decimal
    PRIMARY KEY (classification_id, class_id)
);

-- @table allocation_policy
-- The portfolio's target allocation, from a date. One for the portfolio:
-- each account is judged by its contribution to it (ADR 0048, answer 1). The
-- targets are in `allocation_target`. A missing policy is an error, not a
-- zero (PT-E-ALLOCATION-NO-POLICY).
CREATE TABLE IF NOT EXISTS allocation_policy (
    policy_id      INTEGER PRIMARY KEY,
    effective_from TEXT NOT NULL,
    note           TEXT,
    created_at     TEXT NOT NULL,
    -- The policy of the same date this one corrects (ADR 0025). NULL for an
    -- original. A corrected row is kept and not read.
    corrects_policy_id INTEGER REFERENCES allocation_policy (policy_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_allocation_policy_original
    ON allocation_policy (effective_from) WHERE corrects_policy_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_allocation_policy_corrects
    ON allocation_policy (corrects_policy_id) WHERE corrects_policy_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_allocation_policy_correction_same_key
BEFORE INSERT ON allocation_policy
WHEN NEW.corrects_policy_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM allocation_policy o
    WHERE o.policy_id = NEW.corrects_policy_id
      AND o.effective_from = NEW.effective_from
)
BEGIN
    SELECT RAISE(ABORT, 'a correction names an allocation policy of the same date');
END;

-- @table allocation_target
-- One class's target weight and band in a policy.
CREATE TABLE IF NOT EXISTS allocation_target (
    policy_id INTEGER NOT NULL REFERENCES allocation_policy (policy_id),
    class_id  INTEGER NOT NULL REFERENCES asset_class (class_id),
    -- The target share of the portfolio, from 0 to 1. A policy's targets sum
    -- to exactly 1, cash included.
    target    TEXT NOT NULL,  -- decimal
    -- How far the actual weight may stand from the target, in the same units,
    -- either side: 0.05 is five percentage points (ADR 0048, answer 2). A
    -- weight exactly on the edge is inside.
    band      TEXT NOT NULL,  -- decimal
    PRIMARY KEY (policy_id, class_id)
);

-- Effective-dated and never edited: a mistake is corrected by a row on its own
-- date, never by changing or removing one (ADR 0010, ADR 0025).
CREATE TRIGGER IF NOT EXISTS trg_classification_no_update
BEFORE UPDATE ON classification
BEGIN
    SELECT RAISE(ABORT, 'a classification is not edited: correct it on its date (--corrects)');
END;
CREATE TRIGGER IF NOT EXISTS trg_classification_no_delete
BEFORE DELETE ON classification
BEGIN
    SELECT RAISE(ABORT, 'a classification is not deleted: correct it on its date (--corrects)');
END;
CREATE TRIGGER IF NOT EXISTS trg_classification_part_no_update
BEFORE UPDATE ON classification_part
BEGIN
    SELECT RAISE(ABORT, 'a classification is not edited: correct it on its date (--corrects)');
END;
CREATE TRIGGER IF NOT EXISTS trg_classification_part_no_delete
BEFORE DELETE ON classification_part
BEGIN
    SELECT RAISE(ABORT, 'a classification is not deleted: correct it on its date (--corrects)');
END;
CREATE TRIGGER IF NOT EXISTS trg_allocation_policy_no_update
BEFORE UPDATE ON allocation_policy
BEGIN
    SELECT RAISE(ABORT, 'an allocation policy is not edited: correct it on its date (--corrects)');
END;
CREATE TRIGGER IF NOT EXISTS trg_allocation_policy_no_delete
BEFORE DELETE ON allocation_policy
BEGIN
    SELECT RAISE(ABORT, 'an allocation policy is not deleted: correct it on its date (--corrects)');
END;
CREATE TRIGGER IF NOT EXISTS trg_allocation_target_no_update
BEFORE UPDATE ON allocation_target
BEGIN
    SELECT RAISE(ABORT, 'an allocation policy is not edited: correct it on its date (--corrects)');
END;
CREATE TRIGGER IF NOT EXISTS trg_allocation_target_no_delete
BEFORE DELETE ON allocation_target
BEGIN
    SELECT RAISE(ABORT, 'an allocation policy is not deleted: correct it on its date (--corrects)');
END;

-- ── Seed ────────────────────────────────────────────────────────────────────
INSERT INTO asset_class (name, description, is_builtin, created_at)
VALUES (
    'cash',
    'Cash: each account''s cash balance less its margin loan, sweep vehicles, and '
        || 'dividends declared and not yet paid. Built in.',
    1,
    :applied_at
);

INSERT INTO asset_class (name, description, is_builtin, created_at)
SELECT DISTINCT trim(asset_class), 'From the free-text asset class typed on its instruments '
        || '(migration 13).', 0, :applied_at
FROM instrument
WHERE asset_class IS NOT NULL AND trim(asset_class) <> '' AND trim(asset_class) <> 'cash'
ORDER BY trim(asset_class);

INSERT INTO classification (instrument_id, effective_from, note, created_at)
SELECT
    instrument_id,
    COALESCE((SELECT value FROM meta WHERE key = 'inception_date'), '0001-01-01'),
    'From the free-text asset class (migration 13).',
    :applied_at
FROM instrument
WHERE asset_class IS NOT NULL AND trim(asset_class) <> ''
ORDER BY instrument_id;

INSERT INTO classification_part (classification_id, class_id, weight)
SELECT c.classification_id, a.class_id, '1'
FROM classification c
JOIN instrument i ON i.instrument_id = c.instrument_id
JOIN asset_class a ON a.name = trim(i.asset_class)
ORDER BY c.classification_id;
