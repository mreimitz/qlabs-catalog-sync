"""Real objects for discovery tests to point synthetic entry points at.

Not a ``test_*.py`` file, so pytest never collects it directly; ``conftest.py`` beside it
puts this directory on ``sys.path`` so a test module can build a real
``importlib.metadata.EntryPoint(name=..., value="fixtures:SomeClass", group=...)`` and
call ``.load()`` on it exactly the way ``discovery.py`` does — no monkeypatching of
``discovery`` itself, just a real, loadable module standing in for an installed
connector distribution.

Every ``Connector`` subclass below implements the full abstract surface so it is a
genuinely concrete, instantiable class (not merely a stub with unimplemented
abstractmethods) — discovery never instantiates a connector, but a fixture claiming to
be "a real Connector subclass" should actually be one. The I/O methods simply raise
``NotImplementedError``: no test here ever calls them, only ``discover_connectors``'s
static checks (issubclass, contract version, declared name).
"""

from __future__ import annotations

from typing import Any

from qlabs_catalog_sync_sdk.config import ConnectorConfig
from qlabs_catalog_sync_sdk.contract import (
    SDK_CONTRACT_VERSION,
    CapabilityManifestBase,
    Connector,
    ConnectorContext,
    EntityType,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    NeutralEntity,
    Watermark,
)


class _StubConfig(ConnectorConfig):
    """A field-free ``ConfigModel`` — good enough for a class discovery never instantiates."""


class GoodConnector(Connector):
    """A fully valid connector: real ``Connector`` subclass, current contract version,
    declared ``name`` equal to the entry-point name it will be registered under
    (``"good"`` — set by the test that builds the ``EntryPoint``, not hardcoded here)."""

    name = "good"
    ConfigModel = _StubConfig

    def capabilities(self) -> CapabilityManifestBase:
        raise NotImplementedError

    async def setup(self, ctx: ConnectorContext[Any]) -> None:
        raise NotImplementedError

    async def healthcheck(self) -> HealthStatus:
        raise NotImplementedError

    async def list_changed(self, entity_type: EntityType, since: Watermark) -> ListChangedResult:
        raise NotImplementedError

    async def read(self, ref: IdentityRef) -> NeutralEntity:
        raise NotImplementedError


class AnotherGoodConnector(GoodConnector):
    """A second, independently valid connector class, for multi-connector registry tests."""

    name = "another_good"


class WrongMajorConnector(GoodConnector):
    """A real ``Connector`` subclass built against a contract major this SDK does not
    support — the version-gate rejection case."""

    name = "wrong_major"
    sdk_contract_version = SDK_CONTRACT_VERSION + 1


class MisnamedConnector(GoodConnector):
    """A real, contract-compatible ``Connector`` subclass whose declared ``name`` will
    not match the entry-point name a test registers it under."""

    name = "actual-declared-name"


class NotAConnector:
    """An entry point pointing at the wrong kind of object entirely — not a ``Connector``
    subclass, not even related to the SDK. Simulates a misconfigured entry point value."""
