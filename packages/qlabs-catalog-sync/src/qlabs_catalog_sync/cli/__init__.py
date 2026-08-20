"""Command-line entry point.

WP2 / T2.8. CLI for running the sync engine, including the dry-run mode that computes
and reports planned writes without applying them, and ``identity-confirm``, the wrapper
around T7.1's identity bootstrap/review workflow.

``pyproject.toml`` (not owned by this task) declares the console script as
``qlabs-catalog-sync = "qlabs_catalog_sync.cli:main"``; :func:`main` here is that exact
entry point. See ``cli/app.py`` for the root command group and every subcommand's module
for its own docstring.
"""

from __future__ import annotations

from .app import cli, main

__all__ = ["cli", "main"]
