-- An instrument that will never have an observable market price. ADR 0028,
-- answering ADR 0020 R7.
--
-- A contingent value right, a private placement, a delisted shell: nothing
-- quotes them, and no provider ever will. Valuation had one answer for "no
-- price" -- the holding is missing and the day's snapshot incomplete -- and an
-- incomplete snapshot is never used for a return. So one such holding blocked
-- performance reporting for everything else in its account, for as long as it
-- was held.
--
-- has_market_price = 0 says so explicitly. The holding is valued at zero as a
-- level-5 (subjective, unobservable) valuation, recorded in the snapshot's
-- price set with that provenance (PORT-GIPS-A02, PORT-GIPS-J03), and the
-- snapshot is complete. It is a property of the instrument, not of a date, so
-- it is refused for an instrument that has ever had a price.
--
-- Additive: a new column with a default. No rebuild.

-- 0 when nothing quotes the instrument and nothing will -- a contingent value
-- right. A holding of it is valued at zero as a level-5 valuation, and the
-- snapshot holding it is complete. Refused for an instrument that has ever had
-- a price, because it applies to every date (ADR 0028).
ALTER TABLE instrument ADD COLUMN has_market_price INTEGER NOT NULL DEFAULT 1
    CHECK (has_market_price IN (0, 1));
