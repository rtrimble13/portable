"""Configuration loading, with provenance."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from portable_core.errors import UsageError

__all__ = [
    "DEFAULTS",
    "Config",
    "ConfigValue",
    "Source",
    "load_config",
    "user_config_path",
]

#: Environment variables are prefixed so that `portable`'s configuration is
#: distinguishable from everything else in a shell.
ENV_PREFIX: Final = "PORTABLE_"

#: Filename of the user config, resolved against the home directory when it is
#: needed rather than at import. See :func:`user_config_path`.
USER_CONFIG_NAME: Final = ".portablerc"
PROJECT_CONFIG: Final = Path("portable.toml")


def user_config_path() -> Path | None:
    """The user config file, or None when there is no home directory.

    Resolved lazily, and this is not fussiness. ``Path.home()`` **raises** on
    Windows when neither ``USERPROFILE`` nor ``HOMEDRIVE``/``HOMEPATH`` is set,
    and calling it at module scope made ``import portable_core.config`` fail
    outright in that environment -- taking the whole CLI with it, since
    everything imports config eventually.

    POSIX hides the bug: ``Path.home()`` there falls back to a ``pwd`` lookup
    and succeeds. So this failed only on Windows, which is exactly the platform
    the owner works on.

    A library must not fail to import because of its environment. No home
    directory simply means no user config, which is a perfectly ordinary state
    for a container, a service account, or a locked-down CI runner.
    """
    try:
        return Path.home() / USER_CONFIG_NAME
    except (RuntimeError, OSError):
        return None


class Source(StrEnum):
    """Where a value came from. Reported by `portable config show`."""

    FLAG = "flag"
    ENV = "env"
    PROJECT = "project"
    USER = "user"
    DEFAULT = "default"


#: Built-in defaults. Every key `portable` understands appears here, so this is
#: also the documentation of what is configurable.
DEFAULTS: Final[dict[str, Any]] = {
    # Where the portfolio is, when --port is not given.
    "port": None,
    # Output.
    "format": "table",
    "no_color": False,
    "quiet": False,
    "verbose": 0,
    # Market data.
    "source": "file",
    "offline": False,
    "staleness_tolerance_days": 5,
    # fafnir. The DSN is resolved separately and never stored in a config file
    # when an environment variable is available (bootstrap §6.3).
    "fafnir_dsn": None,
    "duk_path": "duk",
    # File provider.
    "price_file": None,
    "benchmark_file": None,
    # Behaviour.
    "dry_run": False,
    "yes": False,
    "reconcile_tolerance": "0.01",
}

#: Keys that hold a secret or a credential. Never written to a config file by
#: `portable`, never logged, and redacted by `portable config show`.
SECRET_KEYS: Final[frozenset[str]] = frozenset({"fafnir_dsn"})


@dataclass(frozen=True, slots=True)
class ConfigValue:
    """One resolved setting, and where it came from."""

    key: str
    value: Any
    source: Source
    #: The file a file-sourced value came from, for `config show`.
    origin: str | None = None

    @property
    def is_secret(self) -> bool:
        return self.key in SECRET_KEYS

    def display_value(self) -> Any:
        """The value as it may safely be printed."""
        if self.is_secret and self.value is not None:
            return "<redacted>"
        return self.value


@dataclass(frozen=True, slots=True)
class Config:
    """The effective configuration, with provenance for every key."""

    values: Mapping[str, ConfigValue]

    def get(self, key: str, default: Any = None) -> Any:
        entry = self.values.get(key)
        return default if entry is None else entry.value

    def source_of(self, key: str) -> Source:
        entry = self.values.get(key)
        return Source.DEFAULT if entry is None else entry.source

    def to_rows(self) -> list[dict[str, Any]]:
        """For `portable config show`: value **and** where it came from."""
        return [
            {
                "key": key,
                "value": self.values[key].display_value(),
                "source": str(self.values[key].source),
                "origin": self.values[key].origin,
            }
            for key in sorted(self.values)
        ]


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UsageError(
            f"cannot read config file {path}: {exc}",
            code="PT-E-USAGE",
            remedy="Fix the TOML syntax, or move the file aside to use defaults.",
            path=str(path),
        ) from exc
    # A [portable] table is honoured so the file can be shared with other
    # tools; a flat file is honoured so the simple case stays simple.
    section = loaded.get("portable")
    return dict(section) if isinstance(section, dict) else loaded


def _coerce(key: str, raw: str) -> Any:
    """Coerce an environment string to the type its default implies.

    Environment variables are always strings; the default's type is the only
    declaration of intent available, so it is what decides.
    """
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError as exc:
            raise UsageError(
                f"{ENV_PREFIX}{key.upper()} must be an integer, got {raw!r}",
                code="PT-E-USAGE",
                key=key,
            ) from exc
    return raw


def load_config(
    flags: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    user_config: Path | None = None,
    project_config: Path | None = None,
) -> Config:
    """Resolve configuration across all five layers.

    Every argument is injectable so that the resolution order can be tested
    without touching the developer's real environment or home directory --
    which matters, because a config bug that only appears on somebody else's
    machine is the hardest kind to find.
    """
    environment = os.environ if env is None else env
    user_path = user_config_path() if user_config is None else user_config
    project_path = PROJECT_CONFIG if project_config is None else project_config

    resolved: dict[str, ConfigValue] = {
        key: ConfigValue(key, value, Source.DEFAULT) for key, value in DEFAULTS.items()
    }

    if user_path is not None:
        for key, value in _read_toml(user_path).items():
            resolved[key] = ConfigValue(key, value, Source.USER, str(user_path))

    for key, value in _read_toml(project_path).items():
        resolved[key] = ConfigValue(key, value, Source.PROJECT, str(project_path))

    for name, raw in environment.items():
        if not name.startswith(ENV_PREFIX):
            continue
        key = name[len(ENV_PREFIX) :].lower()
        resolved[key] = ConfigValue(key, _coerce(key, raw), Source.ENV, name)

    for key, value in (flags or {}).items():
        # A flag that was not passed arrives as None and must not clobber a
        # lower layer -- otherwise every unspecified flag would erase the
        # config file, which is the classic layered-config bug.
        if value is None:
            continue
        resolved[key] = ConfigValue(key, value, Source.FLAG)

    return Config(values=resolved)
