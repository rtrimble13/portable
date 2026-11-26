"""Asset classes, classification, the allocation policy, and drift. ADR 0048."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import typer

from portable_core.domain.allocation import CASH_CLASS
from portable_core.errors import UsageError
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.services import allocation
from portable_core.services.allocation import NO_TARGET, DriftReport
from portable_pt import state
from portable_pt.commands._shared import dispatch, money_arg, resolve_date

app = typer.Typer(
    help="Asset classes, classification, the allocation policy and drift (ADR 0048).",
    no_args_is_help=True,
)
class_app = typer.Typer(help="The owner's asset classes.", no_args_is_help=True)
target_app = typer.Typer(
    help="The allocation policy: a target and a band per class.", no_args_is_help=True
)
app.add_typer(class_app, name="class")
app.add_typer(target_app, name="target")

EffectiveFrom = Annotated[
    str | None, typer.Option("--effective-from", help="Default: --as-of.")
]
Corrects = Annotated[
    bool,
    typer.Option(
        "--corrects",
        help="Replace the row entered wrongly on this date; it is kept as history.",
    ),
]
Note = Annotated[str | None, typer.Option("--note")]


# ── classes ──────────────────────────────────────────────────────────────────


@class_app.command(name="add")
def add_class(
    name: Annotated[str, typer.Argument(help="The class's name. It never changes.")],
    description: Annotated[str, typer.Option("--description", help="What the class holds.")],
) -> None:
    """Add an asset class. `cash` is built in."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        with ctx.write():
            class_id = allocation.add_class(repos, name, description)
        return CommandResult(
            command="allocation class add",
            data={"class_id": class_id, "name": name.strip(), "description": description},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@class_app.command(name="list")
def list_classes() -> None:
    """The asset classes, `cash` first."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        return CommandResult(
            command="allocation class list",
            table=Table(
                columns=(
                    Column("name", "Class"),
                    Column("description", "Description"),
                    Column("built_in", "Built in", ColumnKind.BOOL),
                ),
                rows=tuple(
                    {"name": c.name, "description": c.description, "built_in": c.is_builtin}
                    for c in repos.allocation.classes()
                ),
                title="Asset classes",
            ),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


# ── classification ───────────────────────────────────────────────────────────


def _shares(specs: list[str]) -> list[tuple[str, Decimal]]:
    """`us_equity` alone, or `us_equity=0.6 us_bonds=0.4`."""
    if len(specs) == 1 and "=" not in specs[0]:
        return [(specs[0].strip(), Decimal(1))]
    shares: list[tuple[str, Decimal]] = []
    for spec in specs:
        if "=" not in spec:
            raise UsageError(
                f"{spec!r}: a split names each share, CLASS=WEIGHT",
                remedy="pt allocation classify SYMBOL us_equity=0.6 us_bonds=0.4",
            )
        name, weight = spec.split("=", 1)
        shares.append((name.strip(), money_arg(weight, what=f"the share of {name}")))
    return shares


@app.command()
def classify(
    symbol: Annotated[str, typer.Argument(help="The instrument, by its symbol on --as-of.")],
    shares: Annotated[
        list[str],
        typer.Argument(help="CLASS for one class, or CLASS=WEIGHT ... summing to 1."),
    ],
    effective_from: EffectiveFrom = None,
    corrects: Corrects = False,
    note: Note = None,
) -> None:
    """Classify an instrument from a date: one class, or a split by weights.

    Declared, never inferred. A balanced fund is split by the weights it
    states, and a target-date fund's glide path is followed by classifying it
    again from a later date. A classification is never edited: one entered
    wrongly is replaced on its own date with --corrects (ADR 0025).
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        instrument = repos.instruments.resolve(symbol, on=ctx.as_of)
        on = resolve_date(effective_from, ctx, what="--effective-from")
        parsed = _shares(shares)
        with ctx.write():
            row_id = allocation.classify(
                repos, instrument, on, parsed, corrects=corrects, note=note
            )
        return CommandResult(
            command="allocation classify",
            data={
                "classification_id": row_id,
                "symbol": instrument.symbol,
                "effective_from": on.isoformat(),
                "shares": [{"asset_class": n, "weight": w} for n, w in parsed],
                "corrects": corrects,
            },
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


# ── the policy ───────────────────────────────────────────────────────────────


def _targets(specs: list[str]) -> list[tuple[str, Decimal, Decimal]]:
    """`us_equity=0.60:0.05`: a target of 60%, five points either side."""
    targets: list[tuple[str, Decimal, Decimal]] = []
    for spec in specs:
        name, _, rest = spec.partition("=")
        target, _, band = rest.partition(":")
        if not name or not target or not band:
            raise UsageError(
                f"{spec!r}: each class is CLASS=TARGET:BAND",
                remedy=(
                    "pt allocation target set us_equity=0.60:0.05 us_bonds=0.35:0.05 "
                    "cash=0.05:0.02 --effective-from DATE"
                ),
            )
        targets.append(
            (
                name.strip(),
                money_arg(target, what=f"the target of {name}"),
                money_arg(band, what=f"the band of {name}"),
            )
        )
    return targets


@target_app.command(name="set")
def set_targets(
    targets: Annotated[
        list[str], typer.Argument(help="CLASS=TARGET:BAND per class, the targets summing to 1.")
    ],
    effective_from: EffectiveFrom = None,
    corrects: Corrects = False,
    note: Note = None,
) -> None:
    """Set the portfolio's target allocation from a date.

    One target for the portfolio, cash included, summing to exactly 1. Each
    band is absolute: 0.05 is five percentage points either side. A change of
    policy does not restate past drift: drift on a date is judged against the
    policy in force that day. A policy is never edited: one entered wrongly is
    replaced on its own date with --corrects (ADR 0025).
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        on = resolve_date(effective_from, ctx, what="--effective-from")
        parsed = _targets(targets)
        with ctx.write():
            policy_id = allocation.set_policy(repos, on, parsed, corrects=corrects, note=note)
        return CommandResult(
            command="allocation target set",
            data={
                "policy_id": policy_id,
                "effective_from": on.isoformat(),
                "targets": [{"asset_class": n, "target": t, "band": b} for n, t, b in parsed],
                "corrects": corrects,
            },
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@target_app.command(name="show")
def show_targets() -> None:
    """The allocation policy in force on --as-of, and every one recorded."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        policy = allocation.policy_on(repos, ctx.as_of)
        return CommandResult(
            command="allocation target show",
            table=Table(
                columns=(
                    Column("asset_class", "Class"),
                    Column("target", "Target", ColumnKind.RATE),
                    Column("band", "Band ±", ColumnKind.RATE),
                ),
                rows=tuple(
                    {"asset_class": t.asset_class, "target": t.target, "band": t.band}
                    for t in policy.targets
                ),
                title=f"Allocation policy in force on {ctx.as_of.isoformat()}, "
                f"from {policy.effective_from.isoformat()}",
            ),
            data={
                "policy_id": policy.policy_id,
                "effective_from": policy.effective_from.isoformat(),
                "history": [
                    {
                        "policy_id": p.policy_id,
                        "effective_from": p.effective_from.isoformat(),
                        "corrects_policy_id": p.corrects_policy_id,
                        "note": p.note,
                    }
                    for p in repos.allocation.policies(include_corrected=True)
                ],
            },
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


# ── drift ────────────────────────────────────────────────────────────────────


@app.command()
def drift(
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Show only this account's mix.")
    ] = None,
    estimate: Annotated[
        str | None,
        typer.Option(
            "--estimate",
            help=(
                "Value a holding with no current price at a carried price (ADR 0044), "
                "if that is at most this share of the total, e.g. 0.05."
            ),
        ),
    ] = None,
) -> None:
    """Drift against the allocation policy in force on --as-of.

    For each class: target, band, value, actual weight, the difference, and
    whether it is outside its band. Valued as `pt mark` values the accounts,
    accrued income included. A holding that cannot be valued leaves every
    weight unknown, so drift is refused unless --estimate allows a carried
    price. Unclassified holdings are named and counted in the total, never
    folded into a class. Read-only: no trade is proposed.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        tolerance = int(ctx.config.get("staleness_tolerance_days", 5))
        max_share = money_arg(estimate, what="--estimate") if estimate is not None else None
        report, warnings = allocation.drift(
            repos,
            ctx.as_of,
            staleness_tolerance_days=tolerance,
            estimate_max_share=max_share,
        )
        if account is not None:
            named = repos.accounts.resolve(account).name
            report_accounts = tuple(a for a in report.accounts if a.account == named)
        else:
            report_accounts = report.accounts
        return CommandResult(
            command="allocation drift",
            table=Table(
                columns=(
                    Column("asset_class", "Class"),
                    Column("target", "Target", ColumnKind.RATE),
                    Column("band", "Band ±", ColumnKind.RATE),
                    Column("value", "Value", ColumnKind.MONEY),
                    Column("weight", "Weight", ColumnKind.RATE),
                    Column("difference", "Difference", ColumnKind.RATE),
                    Column("status", "Status"),
                ),
                rows=tuple(
                    {
                        "asset_class": c.asset_class,
                        "target": c.target,
                        "band": c.band,
                        "value": c.value,
                        "weight": c.weight,
                        "difference": c.difference,
                        "status": c.status,
                    }
                    for c in report.classes
                ),
                title=f"Drift on {ctx.as_of.isoformat()} against the policy from "
                f"{report.policy.effective_from.isoformat()}",
                footnotes=_notes(report, report_accounts),
            ),
            data=_data(report, report_accounts, tolerance, max_share),
            human_data=False,
            warnings=warnings,
            schema_ref="allocation-drift-1.0.json",
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


def _data(
    report: DriftReport,
    accounts: tuple[allocation.AccountMix, ...],
    tolerance: int,
    max_share: Decimal | None,
) -> dict[str, object]:
    return {
        "policy": {
            "policy_id": report.policy.policy_id,
            "effective_from": report.policy.effective_from.isoformat(),
        },
        "total_market_value": report.total,
        "is_complete": report.is_complete,
        "is_estimated": report.is_estimated,
        "outside_band": [c.asset_class for c in report.outside],
        "no_target": list(report.unpoliced),
        "unclassified": [
            {
                "account": u.account,
                "symbol": u.symbol,
                "value": u.value,
                "weight": u.weight,
            }
            for u in report.unclassified
        ],
        "accounts": [
            {
                "account": a.account,
                "total": a.total,
                "unclassified": a.unclassified,
                "classes": [
                    {
                        "asset_class": name,
                        "value": value,
                        "weight": own,
                        "contribution": contribution,
                    }
                    for name, value, own, contribution in a.classes
                ],
            }
            for a in accounts
        ],
        "classified": [
            {"symbol": symbol, "effective_from": on.isoformat()}
            for symbol, on in report.classified
        ],
        "excluded_operating_cash": [
            {"account": name, "amount": amount} for name, amount in report.excluded_cash
        ],
        "estimate": (
            {
                "max_share": max_share,
                "carried": [
                    {
                        "account": c.account,
                        "symbol": c.symbol,
                        "quantity": c.quantity,
                        "price": c.price,
                        "price_date": c.price_date.isoformat(),
                        "age_days": c.age_days,
                        "value": c.value,
                    }
                    for c in report.carried
                ],
            }
            if report.carried
            else None
        ),
        "staleness_tolerance_days": tolerance,
    }


def _notes(report: DriftReport, accounts: tuple[allocation.AccountMix, ...]) -> tuple[str, ...]:
    notes = [
        f"Valued as `pt mark` values the accounts: {report.total:,.2f} in all, accrued "
        "income included. A bond's accrued interest is in its class; a declared "
        f"dividend, and all cash net of any margin loan, in {CASH_CLASS}.",
        "Bands are absolute, either side of the target; a weight exactly on the edge "
        "is inside. Market value, not exposure: an option or a short is held at its "
        "market value in its own class.",
    ]
    if report.unclassified:
        notes.append(
            "Not classified, so in no class but in the total: "
            + "; ".join(
                f"{u.symbol} in {u.account} ({u.value:,.2f})" for u in report.unclassified
            )
            + ". Classify them with `pt allocation classify`."
        )
    if report.unpoliced:
        notes.append(
            f"Held, with no target in the policy: {', '.join(report.unpoliced)} "
            f"({NO_TARGET}). The report is incomplete until the policy names it."
        )
    for name, amount in report.excluded_cash:
        notes.append(
            f"{name} holds operating cash ({amount:,.2f}): left out, as `pt mark` leaves it."
        )
    if report.carried:
        notes.append(
            "ESTIMATED: valued at carried prices (ADR 0044): "
            + "; ".join(
                f"{c.symbol} in {c.account} at {c.price} of {c.price_date.isoformat()} "
                f"({c.age_days} days old)"
                for c in report.carried
            )
            + "."
        )
    for mix in accounts:
        parts = ", ".join(
            f"{name} {contribution:.2%}" for name, _, _, contribution in mix.classes
        )
        notes.append(f"{mix.account} contributes: {parts or 'nothing classified'}.")
    notes.append("No trade is proposed.")
    return tuple(notes)
