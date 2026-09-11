"""Render a custodian's spreadsheet export as CSV, every cell as text.

`pt` reads delimited text and nothing else (docs/broker-import.md §2): the
spreadsheet is a container the custodian chose, and this is the one step that
opens it. Nothing here interprets a cell. Dates become ISO; integers become
their digits; a float in a column named with `--numeric` becomes its shortest
round-trip text, and one that looks like binary noise (more than six decimals,
or an exponent) stops the run rather than being rounded -- the adapter reads
those columns as ``Decimal`` and a rounding here would be a silent one. Every
other column is carried verbatim.

Several files with identical headers concatenate into one CSV, in the order
given. A header mismatch stops the run and names the file. A report's total
line is dropped only when asked (`--require <column>` names a column every
data row fills) and the count of dropped rows is reported.

    python scripts/xlsx_to_csv.py tx-2024.xlsx tx-2025.xlsx \\
        --numeric "Quantity,Amount" -o adapter/transactions.csv

Requires openpyxl, which is not a `portable` dependency: `pip install openpyxl`.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
from pathlib import Path
from typing import Any

_ISO_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}(\.\d+)?Z?")
_MAX_DECIMALS = 6


def render(value: Any, column: str, numeric: frozenset[str]) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        if column not in numeric:
            return text
        if "e" in text or "E" in text:
            raise SystemExit(
                f"{column}: {text} carries an exponent; refusing to render a numeric column "
                f"from a value the spreadsheet stored that way"
            )
        whole, _, fraction = text.partition(".")
        if len(fraction) > _MAX_DECIMALS:
            raise SystemExit(
                f"{column}: {text} has more than {_MAX_DECIMALS} decimals, which is binary "
                f"noise, not a custodian's figure; refusing rather than rounding"
            )
        return whole if fraction in ("", "0") else text
    text = str(value).strip()
    match = _ISO_TIMESTAMP.fullmatch(text)
    if match:
        return match.group(1)
    try:
        # A spelled-out month is unambiguous, so it is rendered ISO. A numeric
        # "01/02/2024" is not -- day-first or month-first is the custodian's
        # convention -- so it is carried as written and `source.toml` declares
        # its format. A calendar date, not an instant: no timezone to attach.
        return dt.datetime.strptime(text, "%b %d, %Y").date().isoformat()  # noqa: DTZ007
    except ValueError:
        return text


def sheet(path: Path) -> tuple[list[str], list[tuple[Any, ...]]]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - environment
        raise SystemExit("openpyxl is required: pip install openpyxl") from exc
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(workbook.worksheets[0].iter_rows(values_only=True))
    if not rows:
        raise SystemExit(f"{path}: the first sheet is empty")
    header = [str(cell).strip() if cell is not None else "" for cell in rows[0]]
    body = [row for row in rows[1:] if any(cell is not None for cell in row)]
    return header, body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "files", nargs="+", type=Path, help="spreadsheets, in the order to concatenate"
    )
    parser.add_argument("-o", "--out", type=Path, required=True, help="the CSV to write")
    parser.add_argument(
        "--numeric",
        default="",
        help="comma-separated headers the adapter reads as numbers; held to the no-noise rule",
    )
    parser.add_argument(
        "--require",
        default=None,
        help=(
            "a header that every data row fills; rows with it blank (a report's total line) "
            "are dropped and counted on stderr"
        ),
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="write each file's rows last-to-first (a custodian that lists newest first)",
    )
    args = parser.parse_args(argv)
    numeric = frozenset(name.strip() for name in args.numeric.split(",") if name.strip())

    header: list[str] | None = None
    written = dropped = 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        for path in args.files:
            file_header, rows = sheet(path)
            if header is None:
                header = file_header
                unknown = numeric - set(header)
                if unknown:
                    raise SystemExit(
                        f"--numeric names columns not in {path}: {sorted(unknown)}"
                    )
                writer.writerow(header)
            elif file_header != header:
                raise SystemExit(
                    f"{path}: headers differ from {args.files[0]}; these files are not one "
                    f"document"
                )
            for row in reversed(rows) if args.reverse else rows:
                cells = list(row) + [None] * (len(header) - len(row))
                if args.require is not None and cells[header.index(args.require)] in (None, ""):
                    dropped += 1
                    continue
                writer.writerow(
                    [
                        render(cell, name, numeric)
                        for cell, name in zip(cells, header, strict=True)
                    ]
                )
                written += 1
    note = f", {dropped} dropped with {args.require!r} blank" if dropped else ""
    print(f"{args.out}: {written} rows from {len(args.files)} file(s){note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
