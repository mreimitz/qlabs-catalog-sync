"""Shared fixtures for the T10.3 config-service test suite: a connector registry with
two fake connectors (one shaped like the Databricks source, one shaped like the sole
Qlik write target, with real secret-typed fields so settings-validation tests have
something to bite on) plus a couple of small, reusable builders.

Named ``config_service_helpers`` rather than ``helpers`` -- this repo collects tests
with pytest's ``importlib`` import mode over one flat module namespace, and that
basename has already collided twice (see ``tests/configstore/configstore_helpers.py``'s
own docstring).

The two fake connectors subclass ``qlabs_catalog_sync_sdk.contract.Connector`` directly
(not the SDK's ``FakeConnector`` test double) and stub every abstract lifecycle/read
method with ``NotImplementedError`` bodies: ``ConfigService`` never instantiates a
connector class -- it only ever reads ``connector_cls.ConfigModel`` off the class
object -- so these are never called, and stubbing them (rather than reusing
``FakeConnector``, whose own ``ConfigModel`` is already narrowed to
``FakeConnectorConfig``) is what lets each fake connector declare its *own*
``ConfigModel`` without mypy flagging a ``ClassVar`` override conflict.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from pydantic import SecretBytes, SecretStr, model_validator

from qlabs_catalog_sync.discovery import ConnectorRegistry
from qlabs_catalog_sync_sdk.config import ConnectorConfig, ConnectorContext
from qlabs_catalog_sync_sdk.contract import (
    CapabilityManifestBase,
    Connector,
    HealthStatus,
    IdentityRef,
    ListChangedResult,
    Watermark,
)
from qlabs_catalog_sync_sdk.models import EntityType, NeutralEntity

#: A fixed instant every test builds writes against, so assertions compare exact
#: values instead of "close enough" timestamps (mirrors ``configstore_helpers.NOW``).
NOW: Final[datetime] = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
LATER: Final[datetime] = datetime(2026, 8, 20, 13, 0, 0, tzinfo=UTC)

#: The actor every test writes as, unless it is specifically testing attribution.
ACTOR: Final[str] = "admin"


class FakeDatabricksConfig(ConnectorConfig):
    """A read-only source connector's config shape: one plain field, no secrets at all
    -- exercises "a connector that declares no secret fields to validate"."""

    host: str


class FakeQlikConfig(ConnectorConfig):
    """The sole v1 write connector's config shape: one plain field, one *required*
    secret (``SecretStr``), one *optional* secret (``SecretBytes``) -- exercises both
    secret wrapper types and both required-ness cases settings validation must handle.
    """

    space_id: str
    api_key: SecretStr
    refresh_token: SecretBytes | None = None


class FakeCredentialRouteConfig(ConnectorConfig):
    """A connector offering two credential routes with a cross-field rule requiring
    exactly one -- the real Databricks connector's shape (OAuth service principal *or*
    personal access token).

    Both credential fields are optional *by type*, because only ever one route is set, so
    neither is ``is_required()`` and neither is given a placeholder during settings
    validation. That combination is what makes the model-level rule unjudgeable from
    ``settings`` alone, and it is the shape that once made such a connector impossible to
    register at all.
    """

    host: str
    token: SecretStr | None = None
    api_key: SecretStr | None = None

    @model_validator(mode="after")
    def _exactly_one_route(self) -> FakeCredentialRouteConfig:
        configured = [name for name in ("token", "api_key") if getattr(self, name) is not None]
        if not configured:
            raise ValueError("configure exactly one credential route: 'token' or 'api_key'")
        if len(configured) > 1:
            raise ValueError("configure only one credential route, not both")
        return self


class _StubConnector(Connector):
    """Every abstract method ``Connector`` declares, stubbed. Never called in this
    suite -- ``ConfigService`` only ever reads ``.ConfigModel`` off the class."""

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


class FakeDatabricksConnector(_StubConnector):
    name = "databricks"
    ConfigModel = FakeDatabricksConfig


class FakeQlikConnector(_StubConnector):
    name = "qlik"
    ConfigModel = FakeQlikConfig


class FakeCredentialRouteConnector(_StubConnector):
    name = "two_routes"
    ConfigModel = FakeCredentialRouteConfig


def make_registry() -> ConnectorRegistry:
    """A registry with exactly the two v1 endpoint roles: ``"databricks"`` (read-only
    source) and ``"qlik"`` (the sole write target, per ``WRITE_CONNECTOR_NAME``)."""
    return ConnectorRegistry(
        {
            "databricks": FakeDatabricksConnector,
            "qlik": FakeQlikConnector,
            "two_routes": FakeCredentialRouteConnector,
        },
        {},
    )
