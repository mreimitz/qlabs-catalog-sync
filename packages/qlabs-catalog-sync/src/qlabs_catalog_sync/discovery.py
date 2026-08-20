"""Entry-point discovery + connector registry + SDK version gate.

WP2 / T2.1. Discovers installed connectors via the ``qlabs_catalog_sync.connectors``
entry-point group (RS-08 section 3), builds a registry keyed by entry-point name — the
stable endpoint-type key that ``EndpointConfig.connector`` (T2.3, ``config.py``), the
IdentityMap, and every log line use — and gates each loaded class against the SDK
contract version (RS-08 section 7, ``qlabs_catalog_sync_sdk.version``) before it is
registered. The engine never imports a connector module directly; this is the only
place that does.

This module is deliberately **synchronous**. ``importlib.metadata.entry_points()`` and
``EntryPoint.load()`` are in-process operations over already-installed package metadata
and already-importable modules — no network I/O, no blocking disk work beyond what
Python's own import machinery already does on every ``import`` statement. Wrapping them
in ``async def`` would buy nothing (there is nothing to ``await`` inside) and would force
every caller, including simple startup/CLI code, into an event loop for no benefit.

Two design decisions this module makes, both worth stating up front because a future
task (T2.4 wiring config to the registry, T2.8 building the engine bootstrap) inherits
them:

1. **Discovery is eager, not lazy.** RS-08 section 3 notes discovery *can* be lazy —
   deferring ``ep.load()`` until a config actually references the name. This module does
   not do that: :func:`discover_connectors` loads and gates every installed connector up
   front, once, at startup. Two reasons. First, the whole point of the version gate and
   the collision check is to fail *at startup*, not partway through a sync cycle when a
   pair first references a broken or incompatible connector — laziness would turn a
   startup problem into a runtime surprise. Second, the MVP installs a small, fixed set
   of connectors (one write connector, up to three read connectors per RM-01/RM-05); the
   cost eager loading defers against is a handful of module imports, not a fleet of
   plugins where startup latency would matter. If that changes, lazy loading is a
   contained follow-up: swap the eager loop below for a cache populated on first
   :meth:`ConnectorRegistry.get_connector` call, keyed by the same entry-point map this
   function already builds from ``entry_points()`` without loading.

2. **A broken connector is reported, not fatal — except a name collision, which is.**
   Three things can go wrong with one installed entry point: ``ep.load()`` itself raises
   (a connector package with an import error or a missing vendor dependency — observable
   in this very monorepo: a placeholder connector package that is registered in
   ``pyproject.toml`` but has not implemented its ``Connector`` subclass yet, e.g. an
   RM-05 connector before its work package lands); the loaded class fails the SDK
   contract-version gate (not a ``Connector`` subclass, no integer version stamp, or a
   mismatched major); or the loaded class's declared ``name`` does not match the
   entry-point name it is registered under. All three are treated the same way: caught,
   logged at error level, and recorded in the registry's ``broken`` map rather than
   raised out of :func:`discover_connectors`. The alternative — crashing discovery
   entirely the moment any one installed connector is broken — would mean a single
   unfinished or misbehaving connector package takes down the whole engine even for pairs
   that never reference it, and would make it impossible to run this monorepo's own dev
   environment (all six packages installed together via ``uv sync --all-packages``)
   before every connector is finished. "Not silently", though: every failure is logged
   immediately and stays queryable via :meth:`ConnectorRegistry.get_connector` /
   :meth:`ConnectorRegistry.broken`, so a config that actually names a broken endpoint
   gets a clear "connector X is installed but broken: <reason>" error distinguishable
   from "connector X is not installed at all" — which is exactly the distinction an
   operator needs and a hard crash at discovery time could not offer once the process
   is already down.

   A **name collision is different and stays fatal.** RS-08 section 3 calls this out
   explicitly: "two packages claiming the same name is a startup error listing both
   distributions." Unlike a broken connector (a problem confined to one entry, that other
   pairs may never touch), two distributions disputing the same entry-point name makes
   the installed environment itself ambiguous — the operator cannot know which
   distribution owns the ``qlik`` key at all until this is resolved, so no part of
   discovery reports something trustworthy about that name. This module checks for
   collisions across every discovered name *before* attempting to load anything, so a
   collision aborts discovery outright rather than getting buried among successfully
   registered connectors.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from importlib.metadata import EntryPoint
from importlib.metadata import entry_points as _installed_entry_points
from typing import Literal

import structlog

from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.version import (
    CONNECTOR_ENTRY_POINT_GROUP,
    SDK_CONTRACT_VERSION,
    ContractVersionError,
    check_contract_compatibility,
)

__all__ = [
    "BrokenConnector",
    "ConnectorBrokenError",
    "ConnectorCollisionError",
    "ConnectorLookupError",
    "ConnectorNotRegisteredError",
    "ConnectorRegistry",
    "discover_connectors",
]

_logger = structlog.get_logger("qlabs.catalog_sync.discovery")

#: The three ways one entry point can fail to become a registered connector. Matches the
#: ``stage`` values :func:`discover_connectors` actually produces; see the module
#: docstring, point 2.
_BrokenStage = Literal["load", "contract", "name_mismatch"]


def _distribution_label(ep: EntryPoint) -> str:
    """A human-readable distribution name for ``ep``, for error messages and logs.

    ``EntryPoint.dist`` is ``None`` for an entry point that ``importlib.metadata``
    could not associate with an installed distribution (not expected in normal
    operation, but not something a diagnostic message should crash over either).
    """
    dist = ep.dist
    name = getattr(dist, "name", None) if dist is not None else None
    return str(name) if name else "<unknown distribution>"


# --------------------------------------------------------------------------------------
# Broken-entry record
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrokenConnector:
    """One entry point that could not become a usable, registered connector.

    Recorded rather than raised — see the module docstring's point 2. ``name`` is the
    entry-point name (the endpoint-type key it would have registered under);
    ``distribution`` is the installed package that claims it; ``stage`` is which check
    rejected it; ``reason`` is a human-readable explanation safe to put directly in a
    config-validation or startup error message.
    """

    name: str
    distribution: str
    stage: _BrokenStage
    reason: str


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class ConnectorCollisionError(Exception):
    """Two or more installed distributions register a connector under the same name.

    Fatal (see module docstring, point 2): raised out of :func:`discover_connectors`
    before any entry point is loaded. ``collisions`` maps every colliding entry-point
    name to the distinct distribution names that claim it (always two or more per key);
    the message already lists all of them, not just the first, so a multi-collision
    installation is fixable in one read rather than one crash per name.
    """

    def __init__(self, message: str, *, collisions: Mapping[str, tuple[str, ...]]) -> None:
        super().__init__(message)
        self.collisions = dict(collisions)


class ConnectorLookupError(LookupError):
    """Base for a :class:`ConnectorRegistry` lookup that could not produce a class.

    Catch this when the caller only needs to know "this endpoint's connector isn't
    usable" without caring which of the two specific reasons (see the subclasses)
    applies; catch the subclasses directly to react to "not installed" and "installed
    but broken" differently, which is the whole reason they are distinct.
    """


class ConnectorNotRegisteredError(ConnectorLookupError):
    """No installed distribution registers a connector under this name at all.

    Distinct from :class:`ConnectorBrokenError`: this means the name never appeared in
    the ``qlabs_catalog_sync.connectors`` entry-point group in the first place (e.g. a
    config typo, or the connector package genuinely is not installed) — there is nothing
    to fix in the connector itself, only in what is installed or what the config names.
    """

    def __init__(self, name: str, *, available: tuple[str, ...]) -> None:
        message = (
            f"no connector is registered under the name {name!r}; installed connectors: "
            f"{list(available)!r}"
            if available
            else f"no connector is registered under the name {name!r} (no connectors "
            "are installed at all)"
        )
        super().__init__(message)
        self.name = name
        self.available = available


class ConnectorBrokenError(ConnectorLookupError):
    """The named connector *is* installed, but discovery could not load/gate/validate it.

    Distinct from :class:`ConnectorNotRegisteredError`: this is a connector-side problem
    (an import error, an SDK contract mismatch, a name/entry-point mismatch) that a
    reinstall of the *engine* will not fix — see :attr:`BrokenConnector.reason` /
    :attr:`BrokenConnector.stage` for what actually went wrong and in which distribution.
    """

    def __init__(self, broken: BrokenConnector) -> None:
        message = (
            f"connector {broken.name!r} is installed (distribution {broken.distribution!r}) "
            f"but is broken: {broken.reason}"
        )
        super().__init__(message)
        self.broken = broken


# --------------------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------------------


class ConnectorRegistry:
    """Connector classes discovered via the entry-point group, keyed by entry-point name.

    Built by :func:`discover_connectors`. The mapping the rest of the engine needs is
    endpoint-key -> connector-name -> class (``EndpointConfig.connector``, T2.3, names
    the connector; the *engine's* endpoint key, e.g. ``"qlik_acme"``, is a separate,
    arbitrary string this registry has no opinion about — see :meth:`get_connector`).
    This module deliberately does not import ``config.py`` to build that association:
    callers pass the connector name they already resolved from config as a plain ``str``.
    """

    def __init__(
        self,
        connectors: Mapping[str, type[Connector]],
        broken: Mapping[str, BrokenConnector],
    ) -> None:
        # Copied defensively so mutating the mapping a caller passed in (or that this
        # object hands back via .broken()) can never reach back into the registry.
        self._connectors: dict[str, type[Connector]] = dict(connectors)
        self._broken: dict[str, BrokenConnector] = dict(broken)

    def get_connector(self, name: str) -> type[Connector]:
        """The connector class registered under ``name`` (an entry-point / connector name).

        Raises :class:`ConnectorBrokenError` when ``name`` was claimed by an installed
        distribution but discovery could not turn it into a usable class, and
        :class:`ConnectorNotRegisteredError` when no installed distribution claims
        ``name`` at all. Config validation (``EndpointConfig.connector`` naming an entry
        point, T2.3/T2.4) needs exactly this distinction to tell an operator "connector
        X is broken" from "connector X is not installed".
        """
        connector_cls = self._connectors.get(name)
        if connector_cls is not None:
            return connector_cls
        broken = self._broken.get(name)
        if broken is not None:
            raise ConnectorBrokenError(broken)
        raise ConnectorNotRegisteredError(name, available=self.names())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._connectors

    def __len__(self) -> int:
        return len(self._connectors)

    def __iter__(self) -> Iterator[str]:
        return iter(self._connectors)

    def names(self) -> tuple[str, ...]:
        """Every successfully registered connector name, in deterministic (sorted) order."""
        return tuple(self._connectors)

    def broken(self) -> Mapping[str, BrokenConnector]:
        """Entry-point names discovery found but could not register, keyed by name."""
        return dict(self._broken)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(connectors={self.names()!r}, "
            f"broken={tuple(self._broken)!r})"
        )


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------


def discover_connectors(
    entry_points: Iterable[EntryPoint] | None = None,
    *,
    sdk_contract_version: int = SDK_CONTRACT_VERSION,
) -> ConnectorRegistry:
    """Enumerate, gate, and register every connector in the entry-point group.

    ``entry_points`` is a testing seam: when ``None`` (the only way engine startup ever
    calls this), the real ``qlabs_catalog_sync.connectors`` entry points installed in
    this environment are enumerated via ``importlib.metadata.entry_points``. Tests pass
    an explicit iterable of real ``importlib.metadata.EntryPoint`` objects instead, so
    they can exercise collisions, broken loads, and version mismatches without needing
    those situations to exist in the real installed environment.

    ``sdk_contract_version`` is forwarded to
    ``qlabs_catalog_sync_sdk.version.check_contract_compatibility`` unchanged; like that
    function's own parameter, overriding it is a testing seam for simulating "this SDK
    supports a different contract major", not something engine startup code should pass.

    Raises :class:`ConnectorCollisionError` immediately (before loading anything) if two
    or more distinct distributions claim the same entry-point name — see the module
    docstring for why this is the one failure mode that aborts discovery outright rather
    than being recorded as :class:`BrokenConnector` and reported via the returned
    registry.
    """
    eps = (
        list(entry_points)
        if entry_points is not None
        else list(_installed_entry_points(group=CONNECTOR_ENTRY_POINT_GROUP))
    )

    by_name: dict[str, list[EntryPoint]] = {}
    for ep in eps:
        by_name.setdefault(ep.name, []).append(ep)

    _raise_on_collisions(by_name)

    connectors: dict[str, type[Connector]] = {}
    broken: dict[str, BrokenConnector] = {}

    for name in sorted(by_name):
        # No collision survived _raise_on_collisions, so every entry point sharing this
        # name resolves to the same distribution; which one we load is arbitrary.
        ep = by_name[name][0]
        distribution = _distribution_label(ep)
        log = _logger.bind(connector=name, distribution=distribution)

        try:
            connector_cls = ep.load()
        except Exception as exc:  # noqa: BLE001 - any import-time failure must be caught here
            log.error(
                "connector_discovery_load_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            broken[name] = BrokenConnector(
                name=name,
                distribution=distribution,
                stage="load",
                reason=f"failed to load {ep.value!r}: {type(exc).__name__}: {exc}",
            )
            continue

        try:
            check_contract_compatibility(connector_cls, sdk_contract_version=sdk_contract_version)
        except ContractVersionError as exc:
            log.error("connector_discovery_contract_incompatible", error=str(exc))
            broken[name] = BrokenConnector(
                name=name, distribution=distribution, stage="contract", reason=str(exc)
            )
            continue

        declared_name = connector_cls.name
        if declared_name != name:
            reason = (
                f"class {connector_cls.__module__}.{connector_cls.__qualname__} declares "
                f"name {declared_name!r}, but is registered under the {name!r} entry point "
                "— config, the IdentityMap and logs would disagree about what to call "
                "this endpoint"
            )
            log.error(
                "connector_discovery_name_mismatch", declared_name=declared_name, reason=reason
            )
            broken[name] = BrokenConnector(
                name=name, distribution=distribution, stage="name_mismatch", reason=reason
            )
            continue

        log.info("connector_discovery_registered")
        connectors[name] = connector_cls

    _logger.info(
        "connector_discovery_complete",
        registered=sorted(connectors),
        broken=sorted(broken),
    )
    return ConnectorRegistry(connectors, broken)


def _raise_on_collisions(by_name: Mapping[str, list[EntryPoint]]) -> None:
    """Raise :class:`ConnectorCollisionError` if any name is claimed by 2+ distributions."""
    collisions: dict[str, tuple[str, ...]] = {}
    for name, group_eps in by_name.items():
        # dict.fromkeys dedupes while preserving first-seen order, so a name claimed
        # twice by the *same* distribution (e.g. a stray duplicate entry-points.txt
        # line) is not treated as a collision.
        distributions = tuple(dict.fromkeys(_distribution_label(ep) for ep in group_eps))
        if len(distributions) > 1:
            collisions[name] = distributions

    if not collisions:
        return

    for name, distributions in sorted(collisions.items()):
        _logger.error(
            "connector_discovery_collision", connector=name, distributions=list(distributions)
        )

    detail = "; ".join(
        f"{name!r} is claimed by distributions {', '.join(repr(d) for d in dists)}"
        for name, dists in sorted(collisions.items())
    )
    raise ConnectorCollisionError(
        f"connector entry-point name collision: {detail}", collisions=collisions
    )
