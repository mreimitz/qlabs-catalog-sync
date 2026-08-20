"""Real manifests, real envelopes and a real connector for the field-diff tests.

Nothing here is a mock. The manifests are built from the SDK's own
:class:`~qlabs_catalog_sync_sdk.manifest.CapabilityManifest` and shaped like the two
endpoints the MVP actually has — a Qlik data-product write surface (JSON Patch, the
closed 8-path enum, 8-operation cap, ETag concurrency, arrays as full replace) and a
read-only Databricks-style source — so a test that passes here is a statement about the
capability contract, not about a stub.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from pydantic import JsonValue

from qlabs_catalog_sync_sdk.config import ConnectorConfig, ConnectorContext
from qlabs_catalog_sync_sdk.contract import (
    Connector,
    HealthStatus,
    ListChangedResult,
    Watermark,
)
from qlabs_catalog_sync_sdk.envelope import build_envelope, build_field_envelopes, order_for
from qlabs_catalog_sync_sdk.manifest import (
    CapabilityManifest,
    ConcurrencyMode,
    EntityCapability,
    FieldCapability,
)
from qlabs_catalog_sync_sdk.models import (
    EntityType,
    FieldEnvelope,
    IdentityRef,
    NeutralEntity,
)

SOURCE_ENDPOINT: Final = "databricks"
TARGET_ENDPOINT: Final = "qlik"
TARGET_REVISION: Final = "etag-42"

QLIK_DATA_PRODUCT_PATHS: Final[list[str]] = [
    "/name",
    "/description",
    "/datasetIds",
    "/glossaryIds",
    "/readMe",
    "/keyContacts",
    "/tags",
    "/apiConsumableDatasetIds",
]
"""The closed JSON Patch path enum Qlik's data-product PATCH accepts (RS-02
``qlik-two-way-sync-readiness.md`` section 2). ``/spaceId`` is deliberately absent: it
appears in the *changelog* vocabulary but not in the PATCH enum, so a connector that
tried to patch it would be refused. Pass ``allowed_update_paths=None`` to
:func:`qlik_manifest` for an endpoint that imposes no closed enum at all."""

QLIK_UPDATE_PATHS: Final[dict[str, str]] = {
    "name": "/name",
    "description": "/description",
    "documentation": "/readMe",
    "tags": "/tags",
    "owners": "/keyContacts",
    "dataset_refs": "/datasetIds",
    "glossary_term_refs": "/glossaryIds",
    "placement": "/spaceId",
}
"""The neutral-to-native translation the Qlik connector owns. ``placement`` maps to a
path the PATCH enum does not accept, which is what makes the ``allowed_update_paths``
rule testable against a real Qlik constraint rather than an invented one."""


def qlik_manifest(
    *,
    allowed_update_paths: Sequence[str] | None = QLIK_DATA_PRODUCT_PATHS,
    max_update_operations: int | None = 8,
    concurrency: ConcurrencyMode = ConcurrencyMode.ETAG,
) -> CapabilityManifest:
    """A Qlik-shaped write manifest for data products.

    Every writable field is ``partial_update=False``: Qlik's PATCH takes only
    ``op: "replace"`` and its arrays are full-replace. ``status`` is ``ro`` (activation
    is a lifecycle action Qlik owns, not a patchable field), ``lineage`` is ``na`` (no
    native equivalent), ``placement`` is ``rw`` but maps outside the path enum, and
    ``custom_attributes`` is not mentioned at all so the fail-closed rule has something
    to fail closed on. ``GLOSSARY_TERM`` is declared unsupported — decision D5.
    """
    writable = FieldCapability.rw(writable_via="rest-patch", partial_update=False)
    return CapabilityManifest(
        entities={
            EntityType.DATA_PRODUCT: EntityCapability(
                supported=True,
                identity_keys=["qri"],
                fields={
                    "name": writable,
                    "description": writable,
                    "documentation": writable,
                    "tags": writable,
                    "owners": writable,
                    "dataset_refs": writable,
                    "glossary_term_refs": writable,
                    "placement": writable,
                    "status": FieldCapability.ro(),
                    "lineage": FieldCapability.na(),
                },
                allowed_update_paths=(
                    None if allowed_update_paths is None else list(allowed_update_paths)
                ),
                max_update_operations=max_update_operations,
            ),
            EntityType.GLOSSARY_TERM: EntityCapability(supported=False, identity_keys=[]),
        },
        concurrency=concurrency,
    )


def readonly_manifest() -> CapabilityManifest:
    """A read-only source manifest: every field ``ro`` or ``na``, never ``rw``.

    Shaped like the Databricks connector under the v1 guardrail that source connectors
    implement read paths only — including D6's ``tags`` being ``na`` when no SQL
    warehouse is configured for the endpoint.
    """
    return CapabilityManifest(
        entities={
            EntityType.DATA_PRODUCT: EntityCapability(
                supported=True,
                identity_keys=["full_name"],
                fields={
                    "name": FieldCapability.ro(),
                    "description": FieldCapability.ro(),
                    "owners": FieldCapability.ro(),
                    "tags": FieldCapability.na(),
                },
            )
        },
        concurrency=ConcurrencyMode.NONE,
    )


def source(values: dict[str, Any]) -> dict[str, FieldEnvelope[JsonValue]]:
    """Freshly read source envelopes, checksummed with each field's real order policy."""
    return build_field_envelopes(values, source_endpoint=SOURCE_ENDPOINT)


def target(
    values: dict[str, Any], *, revision: str | None = TARGET_REVISION
) -> dict[str, FieldEnvelope[JsonValue]]:
    """Last-known target envelopes, all read at one revision."""
    return build_field_envelopes(
        values, source_endpoint=TARGET_ENDPOINT, source_revision=revision
    )


def target_field(
    field: str, value: Any, *, revision: str | None = TARGET_REVISION
) -> FieldEnvelope[JsonValue]:
    """One last-known target envelope, for building a sidecar with mixed revisions."""
    return build_envelope(
        value,
        source_endpoint=TARGET_ENDPOINT,
        order=order_for(field),
        source_revision=revision,
    )


class QlikLikeConnector(Connector):
    """A real :class:`~qlabs_catalog_sync_sdk.contract.Connector` over the Qlik manifest.

    Exists so ``ensure_writable`` — the connector's own belt-and-braces guard — can be
    run against a diff this engine produced, rather than a test asserting against a
    reimplementation of it.
    """

    name = "qlik"
    ConfigModel = ConnectorConfig

    def __init__(self, manifest: CapabilityManifest | None = None) -> None:
        super().__init__()
        self._manifest = qlik_manifest() if manifest is None else manifest

    def capabilities(self) -> CapabilityManifest:
        return self._manifest

    async def setup(self, ctx: ConnectorContext[Any]) -> None:  # pragma: no cover - unused
        return None

    async def healthcheck(self) -> HealthStatus:  # pragma: no cover - unused
        return HealthStatus.healthy(self.name)

    async def list_changed(
        self, entity_type: EntityType, since: Watermark
    ) -> ListChangedResult:  # pragma: no cover - unused
        return ListChangedResult.empty(since)

    async def read(self, ref: IdentityRef) -> NeutralEntity:  # pragma: no cover - unused
        raise NotImplementedError
