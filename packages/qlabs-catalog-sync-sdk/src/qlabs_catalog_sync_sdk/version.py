"""SDK version identifiers, the connector entry-point group, and the discovery-time
compatibility gate.

WP1 / T1.9. RS-08 section 7 draws a hard line between two version numbers that are both
public and both change for different reasons:

* :data:`SDK_CONTRACT_VERSION` — the contract *major*, an integer. It is defined in
  ``contract.py`` (not here) because :class:`~qlabs_catalog_sync_sdk.contract.Connector`
  stamps it onto every connector class as ``sdk_contract_version``; this module only
  re-exports it so callers who want "the version thing" have one obvious place to import
  it from. It changes rarely, only on a breaking contract change, and every connector
  built against contract version *N* must be rejected by an SDK that has moved to a
  different major — that rejection is :func:`check_contract_compatibility` below.
* :data:`CONTRACT_VERSION` — the SDK *package's* full semver (``major.minor.patch``,
  PEP 440). It moves on every release, including additive, backward-compatible ones that
  do not touch :data:`SDK_CONTRACT_VERSION` at all.

RS-08 section 3 shows the engine's discovery loop calling the gate unconditionally,
without inspecting a return value::

    for ep in entry_points(group="qlabs_catalog_sync.connectors"):
        cls = ep.load()
        check_contract_compatibility(cls)   # SDK major-version gate (section 7)
        found[ep.name] = cls

That shape only works if the gate *raises* on rejection and returns plainly on
acceptance — a boolean return the caller forgets to check would let an incompatible
connector through silently, which is exactly the failure mode section 7 exists to
prevent. This module keeps that contract: :func:`check_contract_compatibility` returns
``None`` for a compatible connector and raises :class:`ContractVersionError` for every
rejection case, so the engine's discovery loop (T2.1) can gate a class with a bare
function call.
"""

from __future__ import annotations

from .contract import SDK_CONTRACT_VERSION, Connector

__all__ = [
    "CONNECTOR_ENTRY_POINT_GROUP",
    "CONTRACT_VERSION",
    "SDK_CONTRACT_VERSION",
    "ContractVersionError",
    "check_contract_compatibility",
]


#: The SDK package's own semver (PEP 440 ``major.minor.patch``). Distinct from the
#: integer contract major in :data:`SDK_CONTRACT_VERSION` (RS-08 section 7): this moves
#: on every release, that one only on a breaking contract change.
CONTRACT_VERSION = "0.1.0"

#: The ``importlib.metadata`` entry-point group every connector registers under
#: (RS-08 section 3). The engine enumerates this group at startup to discover
#: connectors; a connector declares it in ``pyproject.toml`` under
#: ``[project.entry-points."qlabs_catalog_sync.connectors"]``.
CONNECTOR_ENTRY_POINT_GROUP = "qlabs_catalog_sync.connectors"


class ContractVersionError(Exception):
    """A connector class the engine loaded cannot be used with this SDK.

    Raised by :func:`check_contract_compatibility` for every rejection case. Deliberately
    **not** part of the :mod:`~qlabs_catalog_sync_sdk.exceptions` hierarchy rooted at
    :class:`~qlabs_catalog_sync_sdk.exceptions.ConnectorError`: those describe a *running*
    connector's operation failing against one endpoint, and carry retry/quarantine/skip
    semantics the engine branches on. This describes a *class* the SDK refuses to load at
    all, before any endpoint, instance, or operation exists — there is no ``endpoint``, no
    ``entity_type``, nothing to retry, and nothing an engine-side handler should do except
    fail startup loudly and name the offending distribution.

    :attr:`connector` identifies the rejected class (its declared
    :attr:`~qlabs_catalog_sync_sdk.contract.Connector.name` when available, else its
    qualified class name), :attr:`built_against` is the contract major it was built
    against (``None`` when that could not even be determined — the not-a-``Connector``
    and missing-stamp cases), and :attr:`sdk_supports` is this SDK's
    :data:`SDK_CONTRACT_VERSION`.
    """

    def __init__(
        self,
        message: str,
        *,
        connector: str,
        built_against: int | None,
        sdk_supports: int,
    ) -> None:
        super().__init__(message)
        self.connector = connector
        self.built_against = built_against
        self.sdk_supports = sdk_supports


def _identify(connector_cls: object) -> str:
    """A human-readable label for an object loaded from the entry-point group.

    Prefers the declared entry-point ``name`` (the identifier that actually appears in
    config, the IdentityMap, and logs) when ``connector_cls`` is a class that carries one;
    falls back to the fully qualified class name, and finally to ``repr()`` for something
    that is not even a class (a module, a function, an instance — anything ``ep.load()``
    could hand back from a misconfigured entry point).
    """
    if not isinstance(connector_cls, type):
        return repr(connector_cls)
    qualname = f"{connector_cls.__module__}.{connector_cls.__qualname__}"
    name = getattr(connector_cls, "name", None)
    return f"{name!r} ({qualname})" if name else qualname


def check_contract_compatibility(
    connector_cls: object,
    *,
    sdk_contract_version: int = SDK_CONTRACT_VERSION,
) -> None:
    """Reject a connector class this SDK cannot safely load (RS-08 section 7).

    Called once per class the engine's discovery loop (T2.1) loads from the
    :data:`CONNECTOR_ENTRY_POINT_GROUP` entry-point group, before that class is
    registered or instantiated. A bare call is the whole gate: this raises
    :class:`ContractVersionError` for every rejection case and returns ``None`` when
    ``connector_cls`` is compatible, matching the RS-08 section 3 discovery loop, which
    calls ``check_contract_compatibility(cls)`` without inspecting a return value.

    Three cases the engine's discovery loop can actually hit, checked in order:

    1. ``connector_cls`` is not a
       :class:`~qlabs_catalog_sync_sdk.contract.Connector` subclass at all — an
       entry point pointing at the wrong object (a module, a function, an unrelated
       class).
    2. it *is* a ``Connector`` subclass, but ``sdk_contract_version`` on it is not an
       ``int`` — unreachable for an ordinary subclass, since the base class always
       stamps an ``int`` default, but reachable if a connector deliberately (and
       wrongly) overrides the class attribute with something else.
    3. its ``sdk_contract_version`` is an ``int`` that differs from
       ``sdk_contract_version`` (this SDK's own :data:`SDK_CONTRACT_VERSION` by
       default) — a connector built against a different contract major.

    ``sdk_contract_version`` is keyword-only and defaults to this SDK's own
    :data:`SDK_CONTRACT_VERSION`; overriding it is a testing seam, not something engine
    code should ever need to do.
    """
    if not (isinstance(connector_cls, type) and issubclass(connector_cls, Connector)):
        raise ContractVersionError(
            f"{_identify(connector_cls)} is not a "
            "qlabs_catalog_sync_sdk.contract.Connector subclass and cannot be loaded "
            "as a connector",
            connector=_identify(connector_cls),
            built_against=None,
            sdk_supports=sdk_contract_version,
        )

    built_against = connector_cls.sdk_contract_version
    if not isinstance(built_against, int):
        raise ContractVersionError(
            f"{_identify(connector_cls)} does not declare an integer "
            "sdk_contract_version and cannot be version-checked",
            connector=_identify(connector_cls),
            built_against=None,
            sdk_supports=sdk_contract_version,
        )

    if built_against != sdk_contract_version:
        raise ContractVersionError(
            f"connector {_identify(connector_cls)} was built against SDK contract "
            f"version {built_against}, but this SDK supports version "
            f"{sdk_contract_version}",
            connector=_identify(connector_cls),
            built_against=built_against,
            sdk_supports=sdk_contract_version,
        )
