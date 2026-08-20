"""Canned capability manifests for :class:`~.fake_connector.FakeConnector`.

WP1 / T1.10. Two realistic shapes, mirroring the two roles a v1 endpoint can play
(RM-01 decision-databricks-to-qlik-mvp.md D1-D8; the v1 scope guardrails in the agent
guide): :func:`qlik_shaped_manifest` — the sole v1 write target, data products fully
described and writable, glossary declared unsupported (D5) — and
:func:`databricks_shaped_manifest` — a read-only source, every field ``ro``/``na``,
never ``rw`` (the guardrail every source connector honors), with the ``tags`` field's
SQL-warehouse gating from decision D6.

These are deliberately *shaped like* the real Qlik/Databricks manifests (T3.2, T4.2)
without being a copy of either connector's actual manifest module — a connector must
never import from another package's connector, and this lives in the SDK, so it can only
ever be a realistic stand-in, not the genuine article. Nothing here is a substitute for
running the conformance kit (T1.8) against the real connectors.
"""

from __future__ import annotations

from ..manifest import CapabilityManifest, ConcurrencyMode, EntityCapability, FieldCapability
from ..models import EntityType

__all__ = [
    "QLIK_DATA_PRODUCT_ALLOWED_UPDATE_PATHS",
    "databricks_shaped_manifest",
    "qlik_shaped_manifest",
]

#: The exact eight JSON Pointer paths Qlik's data-product PATCH accepts (RS-02
#: ``qlik-two-way-sync-readiness.md`` section 2), reused here so
#: :func:`qlik_shaped_manifest` matches the shape T3.2 actually builds.
QLIK_DATA_PRODUCT_ALLOWED_UPDATE_PATHS: tuple[str, ...] = (
    "/name",
    "/description",
    "/datasetIds",
    "/glossaryIds",
    "/readMe",
    "/keyContacts",
    "/tags",
    "/apiConsumableDatasetIds",
)


def qlik_shaped_manifest() -> CapabilityManifest:
    """A Qlik-shaped write manifest: the sole v1 write target.

    Data products are described end to end and writable (``name``, ``description``,
    ``documentation``, ``status``, ``owners``, ``tags``, ``dataset_refs``); ``placement``
    is read-only (moves go through a lifecycle action, not the field-level PATCH path);
    glossary is declared unsupported per decision D5. Datasets are read-only — decision D2
    is that the Qlik connector never creates Qlik datasets, only data products.
    """
    return CapabilityManifest(
        entities={
            EntityType.DATA_PRODUCT: EntityCapability(
                supported=True,
                identity_keys=["id", "qri"],
                fields={
                    "name": FieldCapability.rw(writable_via="rest-patch"),
                    "description": FieldCapability.rw(writable_via="rest-patch"),
                    "documentation": FieldCapability.rw(writable_via="rest-patch"),
                    "status": FieldCapability.rw(writable_via="lifecycle-action"),
                    "owners": FieldCapability.rw(writable_via="rest-patch", partial_update=False),
                    "tags": FieldCapability.rw(writable_via="rest-patch", partial_update=False),
                    "dataset_refs": FieldCapability.rw(
                        writable_via="rest-patch", partial_update=False
                    ),
                    "glossary_term_refs": FieldCapability.na(),
                    "placement": FieldCapability.ro(),
                },
                allowed_update_paths=list(QLIK_DATA_PRODUCT_ALLOWED_UPDATE_PATHS),
                max_update_operations=8,
            ),
            EntityType.DATASET: EntityCapability(
                supported=True,
                identity_keys=["secure_qri"],
                fields={
                    "name": FieldCapability.ro(),
                    "description": FieldCapability.ro(),
                    "physical_ref": FieldCapability.ro(),
                },
            ),
            # Decision D5: glossary is out of the MVP. Declared, not omitted, so a
            # reader can tell this was a choice rather than an oversight.
            EntityType.GLOSSARY_TERM: EntityCapability(supported=False),
        },
        concurrency=ConcurrencyMode.ETAG,
    )


def databricks_shaped_manifest(*, has_sql_warehouse: bool = True) -> CapabilityManifest:
    """A Databricks-shaped read-only manifest.

    Every field is ``ro``/``na``, never ``rw`` — the v1 guardrail every source connector
    honors. Decision D6: ``tags`` needs a SQL warehouse configured even to *read* (it goes
    through ``INFORMATION_SCHEMA`` over the Statement Execution API) — ``ro`` when one is
    configured (the default here), ``na`` when ``has_sql_warehouse=False``.
    """
    tags = FieldCapability.ro() if has_sql_warehouse else FieldCapability.na()
    return CapabilityManifest(
        entities={
            EntityType.DATA_PRODUCT: EntityCapability(
                supported=True,
                identity_keys=["full_name"],
                fields={
                    "name": FieldCapability.ro(),
                    "description": FieldCapability.ro(),
                    "tags": tags,
                    "owners": FieldCapability.ro(),
                },
            ),
            EntityType.DATASET: EntityCapability(
                supported=True,
                identity_keys=["full_name", "object_id"],
                fields={
                    "name": FieldCapability.ro(),
                    "description": FieldCapability.ro(),
                    "tags": tags,
                    "physical_ref": FieldCapability.ro(),
                },
            ),
            # No native glossary at all: never mentioned, not even as `na`.
        },
        concurrency=ConcurrencyMode.NONE,
    )
