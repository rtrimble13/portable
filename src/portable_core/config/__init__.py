"""Layered configuration resolution.

Highest precedence first (bootstrap §6.3):

1. command-line flags
2. environment variables (``PORTABLE_*``)
3. project config (``./portable.toml``)
4. user config (``~/.portablerc``, TOML)
5. built-in defaults

``portable config show --format json`` prints the effective configuration **and
where each value came from**, which is the part that makes a layered scheme
debuggable rather than mysterious.

**What is deliberately not here:** the large-cash-flow and materiality
thresholds. Those are effective-dated rows in the `.port` file
(``return_policy``), because they must be reconstructible for historical
periods -- a threshold that lived in ``~/.portablerc`` could not be recovered
for a period that ended two years ago. ``PORT-GIPS-B03`` and ``E09``. Same
reasoning as tax rates.
"""

from __future__ import annotations

from portable_core.config.settings import (
    DEFAULTS,
    Config,
    ConfigValue,
    Source,
    load_config,
    user_config_path,
)

__all__ = [
    "DEFAULTS",
    "Config",
    "ConfigValue",
    "Source",
    "load_config",
    "user_config_path",
]
