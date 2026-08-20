"""The root command group and the console-script entry point.

WP2 / T2.8. ``pyproject.toml`` (owned by another task; not editable here) declares the
console script as ``qlabs-catalog-sync = "qlabs_catalog_sync.cli:main"``, so
:func:`main` at that exact import path is this package's one hard contract with the
outside world. ``cli/__init__.py`` re-exports it unchanged.

:func:`configure_logging` (T2.7) is called exactly once, here, in the root group's
callback -- "the application's job", per that module's own docstring, and this *is* the
application. Structured logs go to **stderr** (``stream=sys.stderr``) so stdout stays
clean for the human-readable report/plan a person is meant to read; a script piping
stdout to a file or a JSON parser never sees a log line mixed in.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from qlabs_catalog_sync.observability import configure_logging

from .deps import CliDeps, RuntimeContext
from .identity_commands import identity_confirm
from .sync_commands import dry_run, run

__all__ = ["cli", "main"]

_DEFAULT_STATE_DB = "sqlite:///qlabs-catalog-sync.db"
_DEFAULT_REVIEW_FILE = Path("identity-review.json")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@click.group()
@click.option(
    "--state-db",
    envvar="QLABS_STATE_DB",
    default=_DEFAULT_STATE_DB,
    show_default=True,
    help="SQLAlchemy URL for the state store. Migrated to head automatically before every command.",
)
@click.option(
    "--review-file",
    envvar="QLABS_IDENTITY_REVIEW_FILE",
    default=_DEFAULT_REVIEW_FILE,
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Where the identity bootstrap review file lives (shared by every command touching it).",
)
@click.option(
    "--log-level",
    envvar="QLABS_LOG_LEVEL",
    default="INFO",
    show_default=True,
    type=click.Choice(_LOG_LEVELS, case_sensitive=False),
    help="Structured-log level. Logs go to stderr; command output goes to stdout.",
)
@click.pass_context
def cli(ctx: click.Context, state_db: str, review_file: Path, log_level: str) -> None:
    """QLabs Catalog Sync -- one-way metadata sync from source catalogs into Qlik.

    Every command that touches the state store migrates it to head first, so there is
    no separate "run migrations" step before the first cycle.
    """
    configure_logging(level=getattr(logging, log_level.upper()), stream=sys.stderr)
    # An already-supplied CliDeps (a test's `CliRunner.invoke(cli, [...], obj=CliDeps(...))`)
    # is the starting point, never replaced -- see cli/deps.py.
    deps = ctx.obj if isinstance(ctx.obj, CliDeps) else CliDeps()
    ctx.obj = RuntimeContext(state_db=state_db, review_path=review_file, deps=deps)


cli.add_command(run)
cli.add_command(dry_run)
cli.add_command(identity_confirm)


def main() -> None:
    """Console-script entry point (``qlabs-catalog-sync = qlabs_catalog_sync.cli:main``)."""
    cli()
