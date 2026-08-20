"""Programmatic Alembic driving for the state store schema.

There is no ``alembic.ini`` at the repository root or the package root -- T2.2 owns
only ``packages/qlabs-catalog-sync/state/`` and ``packages/qlabs-catalog-sync/alembic/``,
neither of which is the package root, so the config is built in Python instead of read
from an ini file (Alembic supports both; see ``alembic/env.py``). This is what tests
use to run the migration from empty to head on a temp SQLite file, and it is meant to
be what a future startup path (outside this task's scope) calls too.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

__all__ = ["ALEMBIC_DIR", "build_alembic_config", "downgrade_to_base", "upgrade_to_head"]

# The migration scripts live *inside* the installed package, not beside it. The earlier
# location (a sibling of src/) only resolved under an editable checkout: a built wheel put
# the computed path at a directory that had never existed, so any real install failed to
# migrate at all. Keeping them in the package means the same path works in a checkout, a
# wheel and a container alike.
ALEMBIC_DIR = Path(__file__).resolve().parent.parent / "alembic"


def build_alembic_config(url: str) -> Config:
    """An Alembic ``Config`` pointed at this package's migration scripts and ``url``."""
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", url)
    return config


def upgrade_to_head(url: str) -> None:
    """Run every migration up to head against the database at ``url``."""
    command.upgrade(build_alembic_config(url), "head")


def downgrade_to_base(url: str) -> None:
    """Undo every migration against the database at ``url`` (test/dev teardown)."""
    command.downgrade(build_alembic_config(url), "base")
