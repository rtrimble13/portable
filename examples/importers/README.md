# Custodian adapters

An adapter is a directory. For a custodian whose exports are plain tabular
files it contains **no Python at all** — two TOML files that are reviewed as
data, and the exports themselves:

```
<custodian>/
  source.toml        which file is which, how to read its columns, what the
                     adapter claims and the check on data that justifies each
  activity_map.toml  every activity string the custodian emits, mapped once
  holdings.csv       the position statement, with cash
  transactions.csv   the activity history
```

```bash
pt import inspect examples/importers/example-brokerage
```

`example-brokerage/` is a synthetic worked example, exercised by the test suite
so that it cannot drift from the code. Copy the directory, point the column maps
at your custodian's headers, and run `pt import inspect` against your own
export. What comes back is the capability set: what your custodian's files can
support, what they cannot, and what each absence costs.

Read that table before anything else. A portfolio built without `cost_basis` is
permanently different from one built with it, and the difference is not visible
in any later number — `pt tax` simply refuses on pre-cutover lots.

Nothing here is guessed. There is no delimiter sniffing, no date-format
inference, no default arm on the activity map, and no fallback for a column the
file does not have. Every one of those would work for the first custodian and
produce a plausible wrong number for the second.

The full contract, the six checks a capability can name, the sign conventions
and every refusal are in [`docs/broker-import.md`](../../docs/broker-import.md)
§6, decided in [ADR 0018](../../docs/adr/0018-minimum-broker-dataset.md).
