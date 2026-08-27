"""Composable history querying, and the read-only SQL escape hatch.

Treated as a headline feature, not an afterthought (bootstrap §7.1). Every
query respects ``--as-of`` and returns state **as it was known on that date**,
which is what the append-only ledger buys.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

import typer

from portable_core.errors import UsageError, ValidationError
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import open_portfolio
from portable_pt import state
from portable_pt.commands._shared import dispatch, resolve_date

app = typer.Typer(help="Query the ledger and derived state.", no_args_is_help=False)

#: Statements a read-only query may begin with. Anything else is refused
#: before the database sees it -- belt as well as the braces of the read-only
#: connection below.
_READ_ONLY_PREFIXES = ("select", "with", "explain", "pragma table_info", "pragma table_list")

#: Written forms, refused explicitly so the message can say why rather than
#: letting SQLite return a bare "attempt to write a readonly database".
_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|vacuum|reindex)\b",
    re.IGNORECASE,
)


def query(
    sql: Annotated[
        str | None,
        typer.Option(
            "--sql",
            help="A read-only SQL query. Writes are refused, twice over.",
        ),
    ] = None,
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
    symbol: Annotated[str | None, typer.Option("--symbol")] = None,
    txn_type: Annotated[str | None, typer.Option("--type")] = None,
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
    min_amount: Annotated[str | None, typer.Option("--min-amount")] = None,
    note_contains: Annotated[str | None, typer.Option("--note-contains")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 200,
) -> None:
    """Filter the ledger, or drop to read-only SQL with --sql.

    The filter grammar composes: every option narrows further. `--as-of` bounds
    all of them, so a query returns the state as it stood on that date rather
    than today's answer to an old question.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()

        if sql is not None:
            return _sql_query(ctx, sql, limit)

        last = resolve_date(end, ctx, what="--to")
        first = resolve_date(start, ctx, what="--from") if start else None
        account_id = repos.accounts.resolve(account).account_id if account else None
        instrument_id = (
            repos.instruments.resolve(symbol, on=last).instrument_id if symbol else None
        )
        threshold = None
        if min_amount is not None:
            from portable_core.decimals import from_text

            threshold = abs(from_text(min_amount))

        matched = []
        for txn in repos.transactions.in_ledger_order(until=last, account_id=account_id):
            if first is not None and txn.trade_date < first:
                continue
            if instrument_id is not None and txn.instrument_id != instrument_id:
                continue
            if txn_type is not None and str(txn.txn_type) != txn_type:
                continue
            if threshold is not None and abs(txn.net_cash_effect) < threshold:
                continue
            if note_contains and note_contains.lower() not in (txn.note or "").lower():
                continue
            matched.append(txn)

        rows = []
        for txn in matched[-limit:]:
            instrument = repos.instruments.get(txn.instrument_id) if txn.instrument_id else None
            target = repos.accounts.get(txn.account_id)
            rows.append(
                {
                    "txn_id": txn.txn_id,
                    "date": txn.trade_date.isoformat(),
                    "account": target.name if target else None,
                    "type": str(txn.txn_type),
                    "symbol": instrument.symbol if instrument else None,
                    "quantity": txn.quantity,
                    "price": txn.price,
                    "cash": txn.net_cash_effect,
                    "note": txn.note,
                }
            )

        return CommandResult(
            command="query",
            table=Table(
                columns=(
                    Column("txn_id", "Id", ColumnKind.INTEGER),
                    Column("date", "Date", ColumnKind.DATE),
                    Column("account", "Account"),
                    Column("type", "Type"),
                    Column("symbol", "Symbol"),
                    Column("quantity", "Qty", ColumnKind.QUANTITY),
                    Column("price", "Price", ColumnKind.PRICE),
                    Column("cash", "Cash", ColumnKind.MONEY),
                    Column("note", "Note"),
                ),
                rows=tuple(rows),
                title="Query results",
                footnotes=(
                    f"{len(matched)} match(es); showing {len(rows)}. Bounded by "
                    f"--as-of {ctx.as_of.isoformat()}.",
                ),
            ),
            data={"matches": len(matched), "shown": len(rows)},
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


def _sql_query(ctx: Any, sql: str, limit: int) -> CommandResult:
    """Run a read-only query, guarded twice.

    First by inspecting the statement, so the refusal can explain itself; then
    by opening a **separate immutable connection**, so the guard does not
    depend on that inspection being correct. A regex over SQL is a heuristic,
    and a heuristic is not something to stake an append-only ledger on.
    """
    stripped = sql.strip().rstrip(";").strip()
    lowered = stripped.lower()

    if not any(lowered.startswith(prefix) for prefix in _READ_ONLY_PREFIXES):
        raise UsageError(
            "only read-only queries are allowed here",
            remedy=(
                "`pt query --sql` is an escape hatch for reading. The ledger is "
                "append-only and is changed through `pt` commands, which record "
                "why as well as what."
            ),
            statement=stripped[:80],
        )
    if _WRITE_KEYWORDS.search(lowered):
        raise UsageError(
            "this query contains a write statement",
            remedy="Use `pt` commands to change the portfolio.",
            statement=stripped[:80],
        )

    path = str(ctx.config.get("port"))
    connection = open_portfolio(path, read_only=True)
    try:
        cursor = connection.execute(stripped)
        fetched = cursor.fetchmany(limit)
        names = [d[0] for d in cursor.description or []]
    except Exception as exc:
        raise ValidationError(
            f"query failed: {exc}",
            remedy=(
                "`pt query --sql` takes any read-only statement; query "
                "sqlite_master to list the tables, or see docs/schema.md."
            ),
            statement=stripped[:120],
        ) from exc
    finally:
        connection.close()

    return CommandResult(
        command="query --sql",
        table=Table(
            columns=tuple(Column(name, name) for name in names),
            rows=tuple(dict(zip(names, row, strict=True)) for row in fetched),
            title="SQL results",
            footnotes=(
                "Read-only: the connection is opened immutable, so a write cannot "
                "succeed even if the statement check were wrong.",
                "Money columns are canonical decimal TEXT -- comparing or ordering "
                "them as text will not order them as money.",
            ),
        ),
        data={"rows": len(fetched)},
        as_of=ctx.as_of,
        portfolio=ctx.portfolio_name(),
    )
