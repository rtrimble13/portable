"""Layered configuration: precedence, provenance, and secrets."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from portable_core.config import DEFAULTS, FLAG_ONLY, Source, load_config
from portable_core.errors import UsageError
from portable_core.lint._common import repo_root

pytestmark = pytest.mark.unit


def write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_the_five_layers_resolve_in_the_documented_order(tmp_path: Path) -> None:
    user = write(tmp_path / "rc", 'format = "csv"\nsource = "file"\noffline = true\n')
    project = write(tmp_path / "portable.toml", 'source = "fafnir"\n')

    config = load_config(
        {"format": "json"},
        env={"PORTABLE_SOURCE": "null", "PORTABLE_STALENESS_TOLERANCE_DAYS": "10"},
        user_config=user,
        project_config=project,
    )

    assert config.get("format") == "json"  # flag beats user
    assert config.get("source") == "null"  # env beats project
    assert config.get("offline") is True  # user beats default
    assert config.get("staleness_tolerance_days") == 10
    assert config.get("no_color") is False  # untouched default


def test_every_value_reports_where_it_came_from(tmp_path: Path) -> None:
    """A layered scheme without provenance is a layered scheme nobody can debug."""
    user = write(tmp_path / "rc", 'format = "csv"\n')
    config = load_config(
        {"quiet": True},
        env={"PORTABLE_OFFLINE": "1"},
        user_config=user,
        project_config=tmp_path / "missing.toml",
    )

    assert config.source_of("quiet") is Source.FLAG
    assert config.source_of("offline") is Source.ENV
    assert config.source_of("format") is Source.USER
    assert config.source_of("no_color") is Source.DEFAULT


def test_config_show_names_the_file_or_variable_behind_each_row(tmp_path: Path) -> None:
    user = write(tmp_path / "rc", 'duk_path = "/opt/duk"\n')
    config = load_config(
        {"port": "book.port"},
        env={"PORTABLE_SOURCE": "fafnir"},
        user_config=user,
        project_config=tmp_path / "missing.toml",
    )
    rows = {row["key"]: row for row in config.to_rows()}
    assert (rows["port"]["source"], rows["port"]["origin"]) == ("flag", None)
    assert (rows["source"]["source"], rows["source"]["origin"]) == ("env", "PORTABLE_SOURCE")
    assert (rows["duk_path"]["source"], rows["duk_path"]["origin"]) == ("user", str(user))
    assert (rows["price_file"]["source"], rows["price_file"]["value"]) == ("default", None)


# ── what config show may claim takes effect ─────────────────────────────────


def test_config_show_lists_only_what_a_command_reads(tmp_path: Path) -> None:
    """Issue 38. A flag-only key in the list would claim an effect it lacks."""
    user = write(tmp_path / "rc", 'format = "json"\ncolour = "no"\n')
    config = load_config(env={}, user_config=user, project_config=tmp_path / "none.toml")
    assert {row["key"] for row in config.to_rows()} == set(DEFAULTS) - set(FLAG_ONLY)


def test_a_value_that_takes_no_effect_is_reported_as_ignored(tmp_path: Path) -> None:
    """A flag-only key or a typo, from a file or a variable -- never a flag."""
    user = write(tmp_path / "rc", 'format = "json"\ncolour = "no"\nsource = "file"\n')
    config = load_config(
        {"quiet": True},
        env={"PORTABLE_YES": "1", "PORTABLE_DUK_PATH": "duk"},
        user_config=user,
        project_config=tmp_path / "none.toml",
    )
    assert [(entry.key, entry.source) for entry in config.ignored()] == [
        ("colour", Source.USER),
        ("format", Source.USER),
        ("yes", Source.ENV),
    ]


def test_build_options_are_not_read_as_configuration() -> None:
    """PORTABLE_BUILD_NATIVE is for the installer; at run time it is no setting."""
    config = load_config(
        env={"PORTABLE_BUILD_NATIVE": "OFF"},
        user_config=Path("/nonexistent"),
        project_config=Path("/nonexistent"),
    )
    assert "build_native" not in config.values
    assert config.ignored() == []


def test_every_key_a_command_reads_is_declared_and_none_is_left_unread() -> None:
    """What keeps `config show` honest as the code moves.

    Every `config.get("key")` in the source names a key in DEFAULTS that is not
    flag-only, and every such key is read somewhere. `action_file` was read and
    undeclared until this test existed.
    """
    pattern = re.compile(r"config\.get\(\s*\"([a-z_]+)\"")
    read: set[str] = set()
    for path in (repo_root() / "src").rglob("*.py"):
        if path.parent.name == "config":
            continue
        read |= set(pattern.findall(path.read_text(encoding="utf-8")))
    assert read == set(DEFAULTS) - set(FLAG_ONLY)


def test_an_unpassed_flag_does_not_clobber_a_lower_layer(tmp_path: Path) -> None:
    """The classic layered-config bug: every unspecified flag erasing the file.

    Typer hands unspecified options through as None, so None must mean "not
    given" rather than "set to nothing".
    """
    user = write(tmp_path / "rc", 'format = "markdown"\n')
    config = load_config(
        {"format": None, "source": None},
        env={},
        user_config=user,
        project_config=tmp_path / "none.toml",
    )
    assert config.get("format") == "markdown"


def test_environment_values_are_coerced_to_the_defaults_type() -> None:
    """Environment variables are strings; the default's type is the declaration."""
    config = load_config(
        env={
            "PORTABLE_OFFLINE": "true",
            "PORTABLE_VERBOSE": "2",
            "PORTABLE_FORMAT": "csv",
        },
        user_config=Path("/nonexistent"),
        project_config=Path("/nonexistent"),
    )
    assert config.get("offline") is True
    assert config.get("verbose") == 2
    assert config.get("format") == "csv"


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_falsey_environment_strings_are_false(raw: str) -> None:
    config = load_config(
        env={"PORTABLE_OFFLINE": raw},
        user_config=Path("/nonexistent"),
        project_config=Path("/nonexistent"),
    )
    assert config.get("offline") is False


def test_a_non_integer_where_an_integer_is_expected_is_a_usage_error() -> None:
    with pytest.raises(UsageError) as exc:
        load_config(
            env={"PORTABLE_VERBOSE": "loud"},
            user_config=Path("/nonexistent"),
            project_config=Path("/nonexistent"),
        )
    assert exc.value.exit_code == 2


def test_secrets_are_redacted_in_config_show() -> None:
    """A DSN carries a password. `config show` is a thing people paste."""
    config = load_config(
        env={"PORTABLE_FAFNIR_DSN": "host=db user=me password=hunter2"},
        user_config=Path("/nonexistent"),
        project_config=Path("/nonexistent"),
    )
    assert config.get("fafnir_dsn") == "host=db user=me password=hunter2"

    rows = {row["key"]: row for row in config.to_rows()}
    assert rows["fafnir_dsn"]["value"] == "<redacted>"
    assert "hunter2" not in str(config.to_rows())


def test_a_malformed_config_file_is_a_usage_error_not_a_crash(tmp_path: Path) -> None:
    bad = write(tmp_path / "rc", "this is not = = toml\n")
    with pytest.raises(UsageError) as exc:
        load_config(user_config=bad, project_config=tmp_path / "none.toml")
    assert exc.value.exit_code == 2
    assert "move the file aside" in (exc.value.remedy or "")


def test_a_portable_section_and_a_flat_file_both_work(tmp_path: Path) -> None:
    """Shareable with other tools, and simple when it does not need to be."""
    sectioned = write(tmp_path / "a.toml", '[portable]\nformat = "csv"\n')
    flat = write(tmp_path / "b.toml", 'format = "csv"\n')
    for path in (sectioned, flat):
        config = load_config(user_config=path, project_config=tmp_path / "none.toml", env={})
        assert config.get("format") == "csv"


def test_flow_thresholds_are_not_configuration() -> None:
    """PORT-GIPS-B03 and E09.

    They live in the .port file as effective-dated rows, because they must be
    reconstructible for a period that ended two years ago. A threshold in
    ~/.portablerc could not be.
    """
    from portable_core.config.settings import DEFAULTS

    for forbidden in (
        "large_flow_value",
        "large_flow_basis",
        "significant_flow_value",
        "materiality_return_bps",
    ):
        assert forbidden not in DEFAULTS


# ── importable without a home directory ──────────────────────────────────────


def test_the_config_module_imports_without_a_home_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A library must not fail to import because of its environment.

    `Path.home()` **raises** on Windows when neither USERPROFILE nor
    HOMEDRIVE/HOMEPATH is set. Resolving the user config path at module scope
    therefore made `import portable_core.config` fail outright there -- and,
    since everything imports config eventually, took the whole CLI with it.

    POSIX hid it: `Path.home()` there falls back to a `pwd` lookup and
    succeeds. So this passed on Linux and failed only on Windows, which is the
    platform the owner works on. The monkeypatch reproduces the Windows
    behaviour on any platform.
    """
    from portable_core.config.settings import user_config_path

    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)
    assert user_config_path() is None


def test_config_loads_with_no_home_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """No home simply means no user config, which is an ordinary state.

    A container, a service account, and a locked-down CI runner are all
    normal places to run this.
    """

    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)
    config = load_config({"format": "json"}, env={}, project_config=Path("/nonexistent"))

    assert config.get("format") == "json"
    assert config.source_of("format") is Source.FLAG
    assert config.get("no_color") is False


def test_the_fafnir_dsn_lookup_survives_no_home_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same import-time bug existed in the fafnir adapter, which once read
    fafnir's config files under home. Since ADR 0032 it reads none: duk does."""
    from portable_core.providers.fafnir import resolve_dsn

    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)
    monkeypatch.delenv("PORTABLE_FAFNIR_DSN", raising=False)

    assert resolve_dsn() is None
