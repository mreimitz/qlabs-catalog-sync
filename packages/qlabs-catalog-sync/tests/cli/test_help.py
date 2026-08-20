"""`--help` must work for every command and subcommand -- no real config, no real I/O."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from qlabs_catalog_sync.cli import cli

_COMMANDS: list[list[str]] = [
    [],
    ["run"],
    ["dry-run"],
    ["identity-confirm"],
    ["identity-confirm", "bootstrap"],
    ["identity-confirm", "list"],
    ["identity-confirm", "confirm"],
    ["identity-confirm", "reject"],
    ["identity-confirm", "apply"],
]


@pytest.mark.parametrize("args", _COMMANDS, ids=lambda args: " ".join(args) or "<root>")
def test_help(runner: CliRunner, args: list[str]) -> None:
    result = runner.invoke(cli, [*args, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_console_script_entry_point_is_callable() -> None:
    """`main` is what `pyproject.toml`'s console script points at; it must exist and
    resolve to a callable without importing anything that fails at module load time."""
    from qlabs_catalog_sync.cli import main

    assert callable(main)
