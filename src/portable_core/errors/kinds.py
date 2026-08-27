"""The error categories, one per exit code.

Raise the most specific class that fits and give it a stable ``code``. The
module-level constants below name every code `portable` currently raises, so
that ``pt introspect`` can publish them and a test can assert none is a
duplicate.
"""

from __future__ import annotations

from typing import Final

from portable_core.errors.base import ExitCode, PortableError


class UsageError(PortableError):
    """The command was invoked wrongly. Exit 2."""

    exit_code = ExitCode.USAGE
    code = "PT-E-USAGE"


class PortfolioFileError(PortableError):
    """The `.port` file is missing, locked, corrupt, or the wrong version. Exit 3."""

    exit_code = ExitCode.PORTFOLIO
    code = "PT-E-PORTFOLIO"


class ValidationError(PortableError):
    """An invariant was broken, or the request cannot be satisfied honestly. Exit 4.

    This is the "fail loudly on ambiguity" class (``CLAUDE.md`` invariant 9).
    Unmatched lots, a fractional share the account cannot hold, a missing rate
    schedule, a relief-method conflict -- each stops here rather than guessing.
    """

    exit_code = ExitCode.VALIDATION
    code = "PT-E-VALIDATION"


class DataUnavailableError(PortableError):
    """Required data could not be obtained, or is stale beyond tolerance. Exit 5."""

    exit_code = ExitCode.DATA_UNAVAILABLE
    code = "PT-E-DATA-UNAVAILABLE"


class ReconciliationBreakError(PortableError):
    """Holdings or cash disagree with an external extract beyond tolerance. Exit 6."""

    exit_code = ExitCode.RECONCILIATION
    code = "PT-E-RECONCILE-BREAK"


class GipsRefusalError(ValidationError):
    """A refusal that ``docs/gips-standard.md`` requires. Exit 4.

    A distinct class because these refusals are not `portable`'s judgement --
    they are the standard's, and the requirement ID belongs in the context so
    the user can look it up. Where GIPS says "refuse rather than warn"
    (``PORT-GIPS-G01``) or "a missing definition is an error, not a zero"
    (``PORT-GIPS-B03``), this is what that looks like at the boundary.
    """

    code = "PT-E-GIPS"

    def __init__(self, message: str, *, requirement: str, **kwargs: object) -> None:
        super().__init__(message, requirement=requirement, **kwargs)  # type: ignore[arg-type]


# ── The stable code registry ─────────────────────────────────────────────────
# Every code `portable` raises, named once. `pt introspect` publishes this and
# `tests/unit/test_errors.py` asserts the values are unique -- a duplicated
# code is a script somewhere branching on the wrong failure.

E_USAGE: Final = "PT-E-USAGE"
E_PORTFOLIO_NOT_FOUND: Final = "PT-E-PORTFOLIO-NOT-FOUND"
E_PORTFOLIO_EXISTS: Final = "PT-E-PORTFOLIO-EXISTS"
E_PORTFOLIO_LOCKED: Final = "PT-E-PORTFOLIO-LOCKED"
E_PORTFOLIO_CORRUPT: Final = "PT-E-PORTFOLIO-CORRUPT"
E_SCHEMA_TOO_NEW: Final = "PT-E-SCHEMA-TOO-NEW"
E_SCHEMA_TOO_OLD: Final = "PT-E-SCHEMA-TOO-OLD"
E_MIGRATION_FAILED: Final = "PT-E-MIGRATION-FAILED"

E_LEDGER_IMMUTABLE: Final = "PT-E-LEDGER-IMMUTABLE"
E_ACCOUNT_NOT_FOUND: Final = "PT-E-ACCOUNT-NOT-FOUND"
E_ACCOUNT_CLOSED: Final = "PT-E-ACCOUNT-CLOSED"
E_INSTRUMENT_NOT_FOUND: Final = "PT-E-INSTRUMENT-NOT-FOUND"
E_INSTRUMENT_AMBIGUOUS: Final = "PT-E-INSTRUMENT-AMBIGUOUS"
E_POSITION_NOT_FOUND: Final = "PT-E-POSITION-NOT-FOUND"
E_POSITION_CLOSED: Final = "PT-E-POSITION-CLOSED"

E_LOT_UNMATCHED: Final = "PT-E-LOT-UNMATCHED"
E_LOT_INSUFFICIENT: Final = "PT-E-LOT-INSUFFICIENT"
E_LOT_SELECTION_INVALID: Final = "PT-E-LOT-SELECTION-INVALID"
E_TAX_METHOD_CONFLICT: Final = "PT-E-TAX-METHOD-CONFLICT"
E_TAX_NO_RATE_SCHEDULE: Final = "PT-E-TAX-NO-RATE-SCHEDULE"

E_CASH_INSUFFICIENT: Final = "PT-E-CASH-INSUFFICIENT"
E_CASH_NOT_CONSERVED: Final = "PT-E-CASH-NOT-CONSERVED"
E_FRACTIONAL_SHARE: Final = "PT-E-FRACTIONAL-SHARE"
E_FEE_CLASS_MISSING: Final = "PT-E-FEE-CLASS-MISSING"
E_INVARIANT_BROKEN: Final = "PT-E-INVARIANT-BROKEN"
E_REPLAY_MISMATCH: Final = "PT-E-REPLAY-MISMATCH"

E_PRICE_MISSING: Final = "PT-E-PRICE-MISSING"
E_PRICE_STALE: Final = "PT-E-PRICE-STALE"
E_PROVIDER_UNAVAILABLE: Final = "PT-E-PROVIDER-UNAVAILABLE"
E_PROVIDER_CAPABILITY: Final = "PT-E-PROVIDER-CAPABILITY"
E_OFFLINE_CACHE_MISS: Final = "PT-E-OFFLINE-CACHE-MISS"

E_RECONCILE_BREAK: Final = "PT-E-RECONCILE-BREAK"

# Refusals the performance standard requires. See docs/gips-standard.md.
E_GIPS_NO_FLOW_POLICY: Final = "PT-E-GIPS-NO-FLOW-POLICY"
E_GIPS_PRICE_ONLY_BENCHMARK: Final = "PT-E-GIPS-PRICE-ONLY-BENCHMARK"
E_GIPS_ANNUALIZE_SUB_YEAR: Final = "PT-E-GIPS-ANNUALIZE-SUB-YEAR"
E_GIPS_MWR_ALONE: Final = "PT-E-GIPS-MWR-ALONE"
E_GIPS_ADJUSTED_PRICE: Final = "PT-E-GIPS-ADJUSTED-PRICE"
E_GIPS_THEORETICAL_LINK: Final = "PT-E-GIPS-THEORETICAL-LINK"

#: Every code above, for `pt introspect` and for the uniqueness test.
ERROR_CODES: Final[tuple[str, ...]] = (
    E_USAGE,
    E_PORTFOLIO_NOT_FOUND,
    E_PORTFOLIO_EXISTS,
    E_PORTFOLIO_LOCKED,
    E_PORTFOLIO_CORRUPT,
    E_SCHEMA_TOO_NEW,
    E_SCHEMA_TOO_OLD,
    E_MIGRATION_FAILED,
    E_LEDGER_IMMUTABLE,
    E_ACCOUNT_NOT_FOUND,
    E_ACCOUNT_CLOSED,
    E_INSTRUMENT_NOT_FOUND,
    E_INSTRUMENT_AMBIGUOUS,
    E_POSITION_NOT_FOUND,
    E_POSITION_CLOSED,
    E_LOT_UNMATCHED,
    E_LOT_INSUFFICIENT,
    E_LOT_SELECTION_INVALID,
    E_TAX_METHOD_CONFLICT,
    E_TAX_NO_RATE_SCHEDULE,
    E_CASH_INSUFFICIENT,
    E_CASH_NOT_CONSERVED,
    E_FRACTIONAL_SHARE,
    E_FEE_CLASS_MISSING,
    E_INVARIANT_BROKEN,
    E_REPLAY_MISMATCH,
    E_PRICE_MISSING,
    E_PRICE_STALE,
    E_PROVIDER_UNAVAILABLE,
    E_PROVIDER_CAPABILITY,
    E_OFFLINE_CACHE_MISS,
    E_RECONCILE_BREAK,
    E_GIPS_NO_FLOW_POLICY,
    E_GIPS_PRICE_ONLY_BENCHMARK,
    E_GIPS_ANNUALIZE_SUB_YEAR,
    E_GIPS_MWR_ALONE,
    E_GIPS_ADJUSTED_PRICE,
    E_GIPS_THEORETICAL_LINK,
)
