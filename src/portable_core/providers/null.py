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
from portable_core.errors.kinds import E_OFFLINE_CACHE_MISS, E_PROVIDER_UNAVAILABLE
from portable_core.providers.base import Capability, MarketDataProvider

__all__ = ["NullProvider", "OfflineProvider"]


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
    def lookup_security(self, symbol: str, *, on: date) -> Instrument:  # noqa: ARG002

        raise self._refuse(f"the security master lookup for {symbol!r}")

    def eod_prices(self, symbol: str, start: date, end: date) -> list[Price]:  # noqa: ARG002
        raise self._refuse(f"end-of-day prices for {symbol!r}")


class OfflineProvider(NullProvider):
    """What `--offline` gets instead of the configured provider. Issue #39.

    Supplies nothing, so nothing can reach a network: `pt value`, `pt mark`
    and `pt holdings` read prices from the `.port` cache and never ask a
    provider, while a command whose job is to fetch -- `pt price load`,
    `pt ca sync`, `pt instrument sync` -- is refused before any connection is
    attempted. The refusal is its own error, because its remedy is not a
    provider outage's: the data is not in the cache, and the session declined
    to go and get it.
    """

    name = "offline"

    def __init__(self, configured: str) -> None:
        #: The provider `--offline` stood in for, named in the refusal.
        self.configured = configured

    def unavailable(self, capability: Capability) -> DataUnavailableError | None:
        return DataUnavailableError(
            f"--offline: {capability} would have to come from the {self.configured} "
            "provider, and this session uses only what the .port file has cached",
            code=E_OFFLINE_CACHE_MISS,
            remedy=(
                "Drop --offline to fetch it, or set a price by hand with `pt price set`. "
                "Commands that read the cache -- value, mark, holdings -- work offline."
            ),
            provider=self.name,
            configured=self.configured,
            capability=str(capability),
        )

    def _refuse(self, what: str) -> DataUnavailableError:
        return DataUnavailableError(
            f"--offline: {what} would have to come from the {self.configured} provider",
            code=E_OFFLINE_CACHE_MISS,
            remedy="Drop --offline to fetch it.",
            provider=self.name,
            configured=self.configured,
        )
