"""QLabs Catalog Sync SDK — the public contract for the sync engine and connectors.

WP1 (`qlabs-catalog-sync-sdk`). This package is the single public surface: it defines
the neutral metadata model, the connector contract (ABC), the capability manifest,
shared HTTP/auth helpers, the field envelope/checksum utilities, config/exception
types, and the conformance kit. Connectors and the engine import everything from here;
the SDK depends on neither the engine nor any connector.

T1.9 re-exports the full public surface at the package root from the sibling modules
that WP1 has landed. Every name below is imported from exactly one module even where a
module re-exports something it does not itself define (e.g. ``contract.py`` re-exports
``models.EntityType`` for connector authors' convenience) — this module always imports
each symbol from its single authoritative definition site to avoid a duplicate,
ruff-flagged rebinding of the same name.

Two names are deliberately narrowed at this boundary rather than re-exported twice under
one name:

* ``auth.py`` and ``config.py`` each define their own ``Clock``/``SystemClock`` — two
  genuinely different types that happen to share a name (``auth.Clock`` needs only
  ``now()``; ``config.Clock`` also needs ``async sleep()`` and is what
  :class:`~qlabs_catalog_sync_sdk.config.ConnectorContext` carries). Re-exporting both
  under the bare names ``Clock``/``SystemClock`` would make ``from qlabs_catalog_sync_sdk
  import Clock`` pick one silently depending on import order — a public-surface trap.
  This module re-exports ``config``'s versions (the ones ``ConnectorContext`` and most
  connector code actually see); ``auth``'s narrower protocol/impl are still available,
  unambiguously, as ``qlabs_catalog_sync_sdk.auth.Clock`` /
  ``qlabs_catalog_sync_sdk.auth.SystemClock`` for auth-provider code that only needs
  ``now()``. A ``config.SystemClock`` instance satisfies ``auth.Clock`` structurally
  (it has ``now()``), so this loses no capability in practice.

``manifest.py``'s concrete ``CapabilityManifest`` (and the ``EntityCapability`` /
``FieldCapability`` types it is built from) is exported here too: it is the type
``Connector.capabilities()`` returns in practice, and the engine plans every write from
it.
"""

from __future__ import annotations

from .auth import (
    DEFAULT_REFRESH_MARGIN,
    ApiKeyAuthProvider,
    AuthProvider,
    JWTAuthProvider,
    OAuth2ClientCredentialsProvider,
    TokenRequestEncoding,
    TokenTransport,
)
from .config import (
    Clock,
    ConnectorConfig,
    ConnectorContext,
    ManualClock,
    MetricsHandle,
    NullMetrics,
    SystemClock,
)
from .contract import (
    CapabilityManifestBase,
    ChangeKind,
    ChangeRef,
    Connector,
    HealthState,
    HealthStatus,
    ListChangedResult,
    Watermark,
    WatermarkKind,
    WriteOutcome,
    WriteResult,
)
from .envelope import (
    CHECKSUM_ALGORITHM,
    CHECKSUM_PREFIX,
    ORDER_INSENSITIVE_FIELDS,
    TIMESTAMP_PRECISION_DIGITS,
    ArrayOrder,
    CanonicalizationError,
    build_envelope,
    build_field_envelopes,
    canonical_json,
    canonicalize,
    changed_fields,
    compute_checksum,
    has_changed,
    order_for,
    refresh_checksum,
    to_json_value,
)
from .exceptions import (
    AuthError,
    CapabilityError,
    ConflictError,
    ConnectorError,
    NotFound,
    TransientError,
)
from .http import AuthHeaderProvider, HttpEndpoint
from .logging import REDACTED, get_connector_logger, redact_secrets
from .manifest import (
    CapabilityManifest,
    ConcurrencyMode,
    EntityCapability,
    FieldCapability,
    FieldCapabilityMode,
)
from .models import (
    AssetLink,
    AssetType,
    Category,
    DataProduct,
    DataProductStatus,
    Dataset,
    EntityType,
    FieldChange,
    FieldDiff,
    FieldEnvelope,
    FieldUpdateMode,
    GlossaryTerm,
    GlossaryTermStatus,
    IdentityRef,
    NeutralEntity,
    NeutralModel,
    Party,
    PartyRole,
    Tag,
    TermRelation,
    TextField,
    TextFormat,
)
from .version import (
    CONNECTOR_ENTRY_POINT_GROUP,
    CONTRACT_VERSION,
    SDK_CONTRACT_VERSION,
    ContractVersionError,
    check_contract_compatibility,
)

__all__ = [
    "CapabilityManifest",
    "ConcurrencyMode",
    "EntityCapability",
    "FieldCapability",
    "FieldCapabilityMode",
    "CHECKSUM_ALGORITHM",
    "CHECKSUM_PREFIX",
    "CONNECTOR_ENTRY_POINT_GROUP",
    "CONTRACT_VERSION",
    "DEFAULT_REFRESH_MARGIN",
    "ORDER_INSENSITIVE_FIELDS",
    "REDACTED",
    "SDK_CONTRACT_VERSION",
    "TIMESTAMP_PRECISION_DIGITS",
    "ApiKeyAuthProvider",
    "ArrayOrder",
    "AssetLink",
    "AssetType",
    "AuthError",
    "AuthHeaderProvider",
    "AuthProvider",
    "CanonicalizationError",
    "CapabilityError",
    "CapabilityManifestBase",
    "Category",
    "ChangeKind",
    "ChangeRef",
    "Clock",
    "ConflictError",
    "Connector",
    "ConnectorConfig",
    "ConnectorContext",
    "ConnectorError",
    "ContractVersionError",
    "DataProduct",
    "DataProductStatus",
    "Dataset",
    "EntityType",
    "FieldChange",
    "FieldDiff",
    "FieldEnvelope",
    "FieldUpdateMode",
    "GlossaryTerm",
    "GlossaryTermStatus",
    "HealthState",
    "HealthStatus",
    "HttpEndpoint",
    "IdentityRef",
    "JWTAuthProvider",
    "ListChangedResult",
    "ManualClock",
    "MetricsHandle",
    "NeutralEntity",
    "NeutralModel",
    "NotFound",
    "NullMetrics",
    "OAuth2ClientCredentialsProvider",
    "Party",
    "PartyRole",
    "SystemClock",
    "Tag",
    "TermRelation",
    "TextField",
    "TextFormat",
    "TokenRequestEncoding",
    "TokenTransport",
    "TransientError",
    "Watermark",
    "WatermarkKind",
    "WriteOutcome",
    "WriteResult",
    "build_envelope",
    "build_field_envelopes",
    "canonical_json",
    "canonicalize",
    "changed_fields",
    "check_contract_compatibility",
    "compute_checksum",
    "get_connector_logger",
    "has_changed",
    "order_for",
    "redact_secrets",
    "refresh_checksum",
    "to_json_value",
]


def __getattr__(name: str) -> object:
    """Expose the ``testing`` and ``conformance`` subpackages without importing them.

    Both pull in ``respx`` and ``pytest``, which are development dependencies the SDK does
    not declare at runtime — importing them eagerly here made ``import
    qlabs_catalog_sync_sdk`` fail outright in any non-development install, which is every
    real deployment. PEP 562 lets them stay reachable as ``sdk.testing`` /
    ``sdk.conformance`` for the tests and connector authors that want them, while a
    production import never touches them.
    """
    if name in ("conformance", "testing"):
        import importlib

        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
