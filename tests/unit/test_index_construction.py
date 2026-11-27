"""Index construction, by hand (ADR 0050 §3 to §5).

Three securities over five sessions, 2024-01-02 to 2024-01-08:

    day          A      B      C
    01-02 Tue   10     20     40
    01-03 Wed   11     20     42
    01-04 Thu   12     18     44
    01-05 Fri   12     19     40
    01-08 Mon   13     20     42

The base level is 100 on 01-02. Every security is declared a non-payer unless
a test says otherwise.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from decimal import localcontext
from typing import Any

import pytest

from portable_core.domain.index import (
    DistributionSchedule,
    Dividend,
    EventType,
    IndexMeta,
    Member,
    Methodology,
    RebalanceRule,
    Reconstitution,
    Schedule,
    SecurityEvent,
    SecurityType,
    Split,
    SyntheticSecurity,
    Weighting,
)
from portable_core.errors import PortableError
from portable_core.services.index_construction import IndexInputs, construct

pytestmark = pytest.mark.unit

DAYS = [date(2024, 1, d) for d in (2, 3, 4, 5, 8)]
BASE = DAYS[0]
PRICES = {1: ["10", "11", "12", "12", "13"], 2: ["20", "20", "18", "19", "20"],
          3: ["40", "42", "44", "40", "42"]}  # fmt: skip
NAMES = {1: "A", 2: "B", 3: "C", 4: "D"}
#: Within the last digits of a 34-digit context: 1/3 is not exact.
CLOSE = D("1e-28")


def _securities(issuers: dict[int, str] | None = None) -> dict[int, SyntheticSecurity]:
    return {
        sid: SyntheticSecurity(
            security_id=sid,
            symbol=name,
            security_type=SecurityType.EQUITY,
            source="test",
            issuer_key=(issuers or {}).get(sid),
        )
        for sid, name in NAMES.items()
    }


def _closes(prices: dict[int, list[str]] = PRICES) -> dict[tuple[int, date], D]:
    return {
        (sid, on): D(p)
        for sid, series in prices.items()
        for on, p in zip(DAYS, series, strict=True)
        if p != ""
    }


def _inputs(
    weighting: Weighting = Weighting.EQUAL,
    rule: RebalanceRule = RebalanceRule.NEVER,
    *,
    members: tuple[Member, ...] | None = None,
    band: D | None = None,
    **overrides: Any,
) -> IndexInputs:
    members = members or tuple(Member(sid) for sid in (1, 2, 3))
    base = IndexInputs(
        meta=IndexMeta("test", "test", BASE, D(100), "USD", "XNYS"),
        securities=_securities(),
        methodologies=[Methodology(1, BASE, weighting, rule, band)],
        reconstitutions=[Reconstitution(1, BASE, members)],
        events=[],
        schedules=[
            DistributionSchedule(sid, sid, BASE, Schedule.NONE, "test") for sid in NAMES
        ],
        trading_days=DAYS,
        closes=_closes(),
        splits=[],
        dividends=[],
        caps={},
    )
    return replace(base, **overrides)


def _levels(inputs: IndexInputs, which: str = "price_level") -> list[D]:
    return [getattr(row, which) for row in construct(inputs).levels]


def _exact(f: Callable[[], D]) -> D:
    with localcontext() as ctx:
        ctx.prec = 34
        return f()


def test_equal_weight_never_rebalanced_by_hand() -> None:
    """Units from 01-02's close: A 100/3/10, B 100/3/20, C 100/3/40. Held:

    01-03  100/3 x (11/10 + 20/20 + 42/40) = 100/3 x 3.15 = 105
    01-04  100/3 x (12/10 + 18/20 + 44/40) = 100/3 x 3.20 = 106.666...
    """
    levels = _levels(_inputs())
    assert levels[0] == D(100)
    assert levels[1] == D(105)
    assert abs(levels[2] - _exact(lambda: D(100) / 3 * D("3.2"))) < CLOSE


def test_equal_weight_rebalanced_daily_is_a_chain_of_mean_returns() -> None:
    """01-03 is 105 either way. Then each day is the mean of the day's returns:

    01-04  105 x (12/11 + 18/20 + 44/42) / 3
    """
    levels = _levels(_inputs(rule=RebalanceRule.DAILY))
    assert levels[1] == D(105)
    expected = _exact(lambda: D(105) * (D(12) / 11 + D(18) / 20 + D(44) / 42) / 3)
    assert abs(levels[2] - expected) < CLOSE
    # And it is not the unrebalanced index (PORT-GIPS-G03).
    assert abs(levels[2] - _levels(_inputs())[2]) > D("0.01")


def test_custom_weights_by_hand() -> None:
    """0.5 A, 0.3 B, 0.2 C, never rebalanced:

    01-03  100 x (0.5 x 1.1 + 0.3 x 1 + 0.2 x 1.05) = 106
    """
    members = (Member(1, D("0.5")), Member(2, D("0.3")), Member(3, D("0.2")))
    levels = _levels(_inputs(Weighting.CUSTOM, members=members))
    assert levels[1] == D(106)


def test_price_weighted_by_hand() -> None:
    """One unit each; the divisor is 70 / 100 = 0.7.

    01-03  (11 + 20 + 42) / 0.7 = 104.2857...
    """
    built = construct(_inputs(Weighting.PRICE))
    assert built.levels[0].divisor == D("0.7")
    assert built.levels[1].price_level == _exact(lambda: D(73) / D("0.7"))


def test_cap_weighted_by_hand_with_a_free_float() -> None:
    """Caps on 01-02: A 1,000, B 3,000, C 6,000, so 0.1, 0.3 and 0.6.

    01-03  100 x (0.1 x 1.1 + 0.3 x 1 + 0.6 x 1.05) = 104

    With half of C floating, its weighting cap is 3,000: 1/7, 3/7, 3/7.
    """
    caps = {(1, BASE): D(1000), (2, BASE): D(3000), (3, BASE): D(6000)}
    built = construct(_inputs(Weighting.CAP, caps=caps))
    assert built.levels[1].price_level == D(104)
    assert {w.security_id: w.target_weight for w in built.weights} == {
        1: D("0.1"),
        2: D("0.3"),
        3: D("0.6"),
    }
    assert {w.security_id: w.weighting_cap for w in built.weights}[3] == D(6000)

    half = (Member(1), Member(2), Member(3, free_float=D("0.5"), free_float_source="test"))
    floated = construct(_inputs(Weighting.CAP, members=half, caps=caps))
    targets = {w.security_id: w.target_weight for w in floated.weights}
    assert targets[3] == _exact(lambda: D(3000) / D(7000))


def test_a_cap_weighted_index_stops_without_a_cap() -> None:
    caps = {(1, BASE): D(1000), (2, BASE): D(3000)}
    with pytest.raises(PortableError) as caught:
        construct(_inputs(Weighting.CAP, caps=caps))
    assert caught.value.code == "PT-E-INDEX-CAP-MISSING"
    assert "C has no market cap on 2024-01-02" in caught.value.message


@pytest.mark.parametrize("weighting", [Weighting.EQUAL, Weighting.CUSTOM, Weighting.CAP,
                                       Weighting.PRICE])  # fmt: skip
def test_a_split_moves_neither_level(weighting: Weighting) -> None:
    """A splits 2-for-1 ex 01-04, from 11 to 5.50, and nothing else moves. Both
    levels hold on 01-04, under every method. A price-weighted index changes
    its divisor; the others double A's units."""
    flat = {1: ["10", "11", "5.5", "5.5", "5.5"], 2: ["20"] * 5, 3: ["40"] * 5}
    members = (
        (Member(1, D("0.5")), Member(2, D("0.3")), Member(3, D("0.2")))
        if weighting is Weighting.CUSTOM
        else None
    )
    caps = {(s, BASE): D(1000) for s in (1, 2, 3)}
    split = Split(1, DAYS[2], D(2), D(1), "test", "x")
    built = construct(
        _inputs(weighting, members=members, closes=_closes(flat), splits=[split], caps=caps)
    )
    day2, day3 = built.levels[1], built.levels[2]
    assert day3.price_level == day2.price_level
    assert day3.total_return_level == day2.total_return_level
    if weighting is Weighting.PRICE:
        assert day3.divisor != day2.divisor
    else:
        a = {(h.security_id, h.from_date): h.units for h in built.holdings}
        assert a[(1, DAYS[2])] == _exact(lambda: 2 * a[(1, BASE)])


def test_a_dividend_is_reinvested_across_the_index() -> None:
    """B goes ex 0.50 on 01-04. Equal weight, never rebalanced. B holds 100/3/20
    units, so the day adds 100/60 x 0.5 = 0.8333... points, and

        tr  = 105 x (price + points) / 105 = price + points   on 01-04.

    Total return over price is the compounded reinvestment, to the last digit.
    """
    schedules = [DistributionSchedule(s, s, BASE, Schedule.NONE, "t") for s in (1, 3, 4)]
    schedules.append(DistributionSchedule(2, 2, BASE, Schedule.IRREGULAR, "t", max_gap_days=30))
    dividend = Dividend(1, 2, DAYS[2], D("0.5"), "test", "x")
    built = construct(_inputs(dividends=[dividend], schedules=schedules))
    day = built.levels[2]
    (reinvested,) = built.reinvestments
    assert reinvested.index_points == day.dividend_points
    assert abs(day.dividend_points - _exact(lambda: D(100) / 3 / 20 * D("0.5"))) < CLOSE
    assert day.total_return_level == _exact(
        lambda: D(105) * (day.price_level + day.dividend_points) / D(105)
    )
    with localcontext() as ctx:
        ctx.prec = 34
        compounded = D(1)
        for row in built.levels[1:]:
            compounded *= 1 + row.dividend_points / row.price_level
        last = built.levels[-1]
        assert abs(last.total_return_level / last.price_level - compounded) < CLOSE
    # The price level does not see it.
    assert _levels(_inputs())[2] == day.price_level


def test_the_rebalancing_rule_is_explicit() -> None:
    """PORT-GIPS-G03: a monthly-rebalanced and an unrebalanced blend over the
    same days give different returns. Sessions across a month end."""
    days = [date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 1), date(2024, 2, 2)]
    prices = {1: ["10", "15", "15", "16"], 2: ["10", "10", "10", "11"]}
    closes = {(s, d): D(p) for s, ps in prices.items() for d, p in zip(days, ps, strict=True)}
    members = (Member(1, D("0.5")), Member(2, D("0.5")))
    common: dict[str, Any] = {
        "meta": IndexMeta("t", "t", days[0], D(100), "USD", "XNYS"),
        "reconstitutions": [Reconstitution(1, days[0], members)],
        "trading_days": days,
        "closes": closes,
    }

    def run(rule: RebalanceRule) -> list[Any]:
        methods = [Methodology(1, days[0], Weighting.CUSTOM, rule)]
        return list(construct(_inputs(**common, methodologies=methods)).levels)

    monthly, never = run(RebalanceRule.MONTHLY), run(RebalanceRule.NEVER)
    assert [r.is_rebalance for r in monthly] == [True, True, False, False]
    assert [r.is_rebalance for r in never] == [True, False, False, False]
    assert monthly[-1].total_return_level != never[-1].total_return_level


def test_a_drift_band_rebalances_only_beyond_its_width() -> None:
    """Equal weight, a 5-point band. On 01-04, C (44/40) and B (18/20) have
    moved: weights 1.2/3.2, 0.9/3.2, 1.1/3.2 = 0.375, 0.281, 0.344. A and B are
    more than 5 points from 1/3, so 01-04 rebalances; 01-03 does not."""
    built = construct(_inputs(rule=RebalanceRule.DRIFT_BAND, band=D("0.05")))
    assert [r.is_rebalance for r in built.levels] == [True, False, True, False, False]


def test_an_undeclared_departure_stops_the_build() -> None:
    gone = {**PRICES, 3: ["40", "42", "44", "", ""]}
    with pytest.raises(PortableError) as caught:
        construct(_inputs(closes=_closes(gone)))
    assert caught.value.code == "PT-E-INDEX-EVENT-UNDECLARED"
    assert "C has no close on 2024-01-05 or after it" in caught.value.message


def test_a_missing_close_stops_the_build() -> None:
    gap = {**PRICES, 3: ["40", "42", "", "40", "42"]}
    with pytest.raises(PortableError) as caught:
        construct(_inputs(closes=_closes(gap)))
    assert caught.value.code == "PT-E-INDEX-CLOSE-MISSING"
    assert "C has no close on 2024-01-04" in caught.value.message


@pytest.mark.parametrize("weighting", [Weighting.EQUAL, Weighting.PRICE])
def test_a_declared_delisting_is_settled_at_what_it_paid(weighting: Weighting) -> None:
    """C is delisted after 01-04's close, paying 44 a share: valued at 44 that
    day, and its proceeds are reinvested (or, price-weighted, the divisor
    absorbs it). The level on 01-04 is the one it would have had."""
    gone = {**PRICES, 3: ["40", "42", "44", "", ""]}
    event = SecurityEvent(1, 3, DAYS[2], EventType.DELISTING, "test", cash_per_share=D(44))
    built = construct(_inputs(weighting, closes=_closes(gone), events=[event]))
    assert built.levels[2].price_level == construct(_inputs(weighting)).levels[2].price_level
    assert [r.member_count for r in built.levels] == [3, 3, 3, 2, 2]


def test_a_spinoff_adds_the_received_shares_at_the_open() -> None:
    """C spins off D, one for one, ex 01-05: C drops 44 to 40, D trades at 4.
    The level is the same as if C had held at 44."""
    prices = {**PRICES, 3: ["40", "42", "44", "40", "42"], 4: ["", "", "", "4", "4"]}
    event = SecurityEvent(
        1, 3, DAYS[3], EventType.SPINOFF, "test", received_security_id=4, received_ratio=D(1)
    )
    built = construct(_inputs(closes=_closes(prices), events=[event]))
    held = {**PRICES, 3: ["40", "42", "44", "44", "46"]}
    assert (
        built.levels[3].price_level
        == construct(_inputs(closes=_closes(held))).levels[3].price_level
    )


def test_custom_weights_are_not_spread_by_guess_after_a_departure() -> None:
    gone = {**PRICES, 3: ["40", "42", "44", "", ""]}
    members = (Member(1, D("0.5")), Member(2, D("0.3")), Member(3, D("0.2")))
    event = SecurityEvent(1, 3, DAYS[2], EventType.DELISTING, "test", cash_per_share=D(44))
    with pytest.raises(PortableError) as caught:
        construct(
            _inputs(
                Weighting.CUSTOM,
                RebalanceRule.DAILY,
                members=members,
                closes=_closes(gone),
                events=[event],
            )
        )
    assert caught.value.code == "PT-E-INDEX-DEFINITION"
    assert "C left the index" in caught.value.message


def test_one_issuer_is_held_once() -> None:
    inputs = _inputs(securities=_securities({1: "CIK1", 2: "CIK1"}))
    with pytest.raises(PortableError) as caught:
        construct(inputs)
    assert caught.value.code == "PT-E-INDEX-ONE-ISSUER"


def test_every_security_held_declares_how_it_pays() -> None:
    schedules = [DistributionSchedule(s, s, BASE, Schedule.NONE, "t") for s in (1, 2)]
    with pytest.raises(PortableError) as caught:
        construct(_inputs(schedules=schedules))
    assert caught.value.code == "PT-E-INDEX-NO-SCHEDULE"
    assert "C is held on 2024-01-02" in caught.value.message


def test_a_dividend_from_a_declared_non_payer_stops_the_build() -> None:
    dividend = Dividend(1, 2, DAYS[2], D("0.5"), "test", "x")
    with pytest.raises(PortableError) as caught:
        construct(_inputs(dividends=[dividend]))
    assert caught.value.code == "PT-E-INDEX-DIVIDEND-GAP"


def test_a_gap_longer_than_the_schedule_allows_stops_the_build() -> None:
    """B is declared quarterly and its last dividend went ex 2023-08-01: 154
    days before 01-02, more than the 120 a quarterly payer may show."""
    schedules = [DistributionSchedule(s, s, BASE, Schedule.NONE, "t") for s in (1, 3, 4)]
    schedules.append(DistributionSchedule(2, 2, BASE, Schedule.QUARTERLY, "t"))
    old = Dividend(1, 2, date(2023, 8, 1), D("0.5"), "test", "x")
    with pytest.raises(PortableError) as caught:
        construct(_inputs(schedules=schedules, dividends=[old]))
    assert caught.value.code == "PT-E-INDEX-DIVIDEND-GAP"
    assert caught.value.context["gap_from"] == "2023-08-01"
    recent = Dividend(1, 2, date(2023, 11, 1), D("0.5"), "test", "x")
    construct(_inputs(schedules=schedules, dividends=[recent]))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"methodologies": [Methodology(1, DAYS[1], Weighting.EQUAL, RebalanceRule.NEVER)]},
         "no methodology takes effect on the base date"),
        ({"reconstitutions": [Reconstitution(1, DAYS[1], (Member(1),))]},
         "no membership takes effect on the base date"),
        ({"meta": IndexMeta("t", "t", date(2024, 1, 1), D(100), "USD", "XNYS")},
         "is not a trading day"),
    ],
)  # fmt: skip
def test_a_definition_that_cannot_start_is_refused(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(PortableError, match=message) as caught:
        construct(_inputs(**change))
    assert caught.value.code == "PT-E-INDEX-DEFINITION"


def test_two_builds_of_the_same_inputs_are_the_same() -> None:
    inputs = _inputs(rule=RebalanceRule.DAILY)
    assert construct(inputs) == construct(inputs)
