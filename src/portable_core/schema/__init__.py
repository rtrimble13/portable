"""Versioned DDL and the migration machinery.

The schema lives in numbered `.sql` files in this package rather than in
strings buried in Python (ADR 0002), so that:

* ``docs/schema.md`` can be generated from the DDL comments and cannot drift;
* a migration is reviewable as SQL by somebody who does not read Python;
* the checksum of what was applied is meaningful.
"""

from __future__ import annotations

from portable_core.schema.migrations import (
    CURRENT_SCHEMA_VERSION,
    Migration,
    applied_versions,
    available_migrations,
    initialise,
    migrate,
    schema_version,
    split_statements,
    verify_checksums,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "Migration",
    "applied_versions",
    "available_migrations",
    "initialise",
    "migrate",
    "schema_version",
    "split_statements",
    "verify_checksums",
]
