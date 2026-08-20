"""The CLI's shared, per-invocation state and its one test seam.

WP2 / T2.8. :class:`RuntimeContext` is what every subcommand receives as ``ctx.obj``
(``@click.pass_obj``): the state-store URL and identity review-file path parsed from the
root group's options, plus :class:`CliDeps` -- the one thing a test may override.

Every subcommand builds its connectors from ``qlabs_catalog_sync.discovery.
discover_connectors()`` in production, which walks the real
``qlabs_catalog_sync.connectors`` entry points installed in the environment. A CLI test
has no real Databricks/Qlik tenant to discover, so it needs to substitute the SDK's
``FakeConnector`` for both. Rather than monkeypatching ``discovery`` (which would also
hide a real discovery bug) or teaching every wiring function a second "or pass connectors
directly" parameter, the seam is exactly the registry discovery already returns: a test
builds a :class:`~qlabs_catalog_sync.discovery.ConnectorRegistry` mapping connector names
(``"qlik"``, ``"databricks"``, ...) to connector *classes* -- precisely what real
entry-point discovery would have produced -- and passes it as
``CliRunner.invoke(cli, [...], obj=CliDeps(registry=...))``. The root group command
(``cli/app.py``) treats an already-supplied :class:`CliDeps` as the starting point rather
than replacing it, so the injected registry survives into every subcommand's
``RuntimeContext``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from qlabs_catalog_sync.discovery import ConnectorRegistry, discover_connectors

__all__ = ["CliDeps", "RuntimeContext"]


@dataclass(slots=True)
class CliDeps:
    """Dependencies a caller (production code or a test) may fix in advance.

    ``registry``, when given, is used as-is instead of calling
    :func:`~qlabs_catalog_sync.discovery.discover_connectors`. Production code never
    sets this; only tests do.
    """

    registry: ConnectorRegistry | None = None


@dataclass(slots=True)
class RuntimeContext:
    """Per-invocation context built once by the root group and shared by every subcommand."""

    state_db: str
    review_path: Path
    deps: CliDeps = field(default_factory=CliDeps)

    def connector_registry(self) -> ConnectorRegistry:
        """The registry to build connectors from: the injected one, or a real discovery."""
        if self.deps.registry is not None:
            return self.deps.registry
        return discover_connectors()
