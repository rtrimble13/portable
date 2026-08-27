"""The provider that refuses politely.

Its whole job is to make a command that needs prices fail with a good message
rather than an ``ImportError`` at CLI start or an ``AttributeError`` three
frames deep. It is the default when no source is configured, so the first thing
a new user sees when they run `pt value` with nothing set up is a sentence
telling them what to do.
"""

from __future__ import annotations

from datetime import date

from portable_core.domain.models import Instrument, Price
from portable_core.errors import DataUnavailableError
from portable_core.errors.kinds import E_PROVIDER_UNAVAILABLE
from portable_core.providers.base import Capability, MarketDataProvider

__all__ = ["NullProvider"]


class NullProvider(MarketDataProvider):
    """Declares nothing, supplies nothing, and says so clearly."""

    name = "null"

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset()

    def _refuse(self, what: str) -> DataUnavailableError:
        return DataUnavailableError(
            f"no market data provider is configured, so {what} is unavailable",
            code=E_PROVIDER_UNAVAILABLE,
            remedy=(
                "Choose a source with --source file (and --price-file PATH) or "
                "--source fafnir, or set it in ~/.portablerc. Prices already cached "
                "in the .port file are still usable with --offline; you can also set "
                "one by hand with `pt price set`."
            ),
            provider=self.name,
        )

    # The unused parameters are the protocol's, not ours: a provider that
    # refuses still has to have the shape of one.
    def lookup_security(self, symbol: str, *, on: date | None = None) -> Instrument:  # noqa: ARG002

        raise self._refuse(f"the security master lookup for {symbol!r}")

    def eod_prices(self, symbol: str, start: date, end: date) -> list[Price]:  # noqa: ARG002
        raise self._refuse(f"end-of-day prices for {symbol!r}")
