"""Databricks capability manifest.

WP4 / T4.2. Declares this connector's capability honestly for the two Unity Catalog
object kinds it can see: a UC **schema** as ``EntityType.DATA_PRODUCT`` and a **table or
view** inside that schema as ``EntityType.DATASET`` (decision D1 — see
``planning/Roadmap/completed/RM-01-one-way-sync-mvp/decision-databricks-to-qlik-mvp.md`` item 1).
Databricks and Snowflake shares/listings ("data products" in the marketplace sense) are
deferred alongside them — D1 confines the mapping to ``catalog.schema``.

**Read-only, everywhere.** This is a v1 source connector (agent guide "v1 scope
guardrails"): every field declared below is ``ro`` or ``na``, never ``rw``, and no field
carries ``writable_via`` or ``allowed_update_paths``. The inherited ``Connector``
write-method defaults (``create``/``update``/``delete``) already refuse with
``CapabilityError`` regardless of what this manifest says; declaring nothing writable is
what makes that refusal an honest reflection of the manifest rather than an accident of
what ``__init__.py`` happens not to override.

**Identity**: both entities use ``identity_keys = ["full_name", "object_id"]`` — the
three-level qualified name (RS-01 section 1.1/4.1) plus the object's stable id
(``table_id`` for a table/view, the schema id for a schema). Names can be renamed
(``new_name``, RS-01 section 4.1); ids cannot, so both are carried and the id is the
safer join key.

**Concurrency**: ``ConcurrencyMode.NONE``. Databricks exposes no ETag or revision counter
on Unity Catalog objects that this connector could use for optimistic concurrency (there
is nothing to write anyway); change detection for this connector is snapshot + checksum
comparison against the last-read field envelope (RS-03 section 6: "No field-level
provenance, so use snapshot + checksum comparison"), never a server-side token. That is a
structural fact about the Databricks read surface, stated here so the next reader does
not mistake it for an oversight.

**Decision D6 (the conditional field)**: UC object tags are readable only through
``INFORMATION_SCHEMA.*_TAGS`` over the Statement Execution API (RS-01 section 1.3),
which needs a configured SQL warehouse
(:attr:`~qlabs_connector_databricks.config.DatabricksConfig.has_sql_warehouse`). Databricks
also has no separate "classification" concept — RS-03 section 8.2 realizes the neutral
``Dataset.classifications`` field for Databricks through that exact same tag surface
("classifications | RW (tags)"), so ``classifications`` inherits D6's conditionality
too: both ``tags`` and ``classifications`` are ``ro`` when a warehouse is configured and
``na`` otherwise. An honest ``na`` beats a silently empty read.

**What has no manifest entry, and why that is still honest**: ``EntityCapability.fields``
(SDK ``manifest.py``) already treats an entity's neutral field that is simply absent from
this dict exactly like ``na`` — so omission is only a legitimate answer for a neutral
field this manifest chooses not to negotiate. Native lineage, audit/system fields
(``created_at``, ``created_by``, ``updated_at``, ``updated_by``, ``metastore_id``, and any
object id beyond the one carried in ``identity_keys``), and a bare ``storage_location``
(RS-01 sections 1.2, 1.4, 4.3) are excluded outright for a stronger reason than omission:
none of them correspond to *any* attribute the neutral ``DataProduct``/``Dataset`` models
declare (RS-03 section 3), so there is no neutral field to hang a capability on in the
first place — this is "no neutral equivalent at all," not a field we merely chose not to
read. Where a Databricks "structural" fact *does* feed a real neutral field, it is
declared instead: ``table_type``/``data_source_format`` (RS-01 sections 1.2, 4.3) feed
``Dataset.asset_type`` (``ro``), and ``full_name`` feeds ``Dataset.physical_ref`` (``ro``)
in addition to both entities' ``identity_keys``. ``DataProduct`` (the schema) has no
``physical_ref`` field at all — RS-03 section 8.1's neutral-to-endpoint table carries no
``physicalRef`` row for ``DataProduct`` the way section 8.2's does for ``Dataset`` — so
for the schema, ``full_name`` is only ever an identity key, never a data field.
``NeutralEntity.custom_attributes`` (``properties`` on a UC object) is likewise never
given its own ``FieldCapability`` entry, matching every other manifest in this codebase
(see the SDK's own ``tests/manifest/conftest.py`` reference fixtures): it is a
free-form, always-round-tripped sidecar, not a field capability negotiation participates
in.

**Glossary**: ``GLOSSARY_TERM`` and ``CATEGORY`` are declared ``supported=False``, not
omitted. Databricks has no native business glossary or category/domain concept at all
(decision item 5: "Databricks has no native glossary"), so this is a considered "no" —
distinct from Qlik's decision D5, where Qlik *can* support a glossary but the MVP chooses
not to sync one. Declaring it, rather than leaving the entity unmentioned, lets a reader
tell the two situations apart.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.manifest import (
    CapabilityManifest,
    ConcurrencyMode,
    EntityCapability,
    FieldCapability,
)
from qlabs_catalog_sync_sdk.models import EntityType

from .config import DatabricksConfig

__all__ = ["build_manifest", "manifest_for_config"]

#: Both entities key identity the same way (decision D1/RS-01 section 4.1): the
#: three-level qualified name plus the object's stable id.
_IDENTITY_KEYS = ["full_name", "object_id"]


def build_manifest(*, has_sql_warehouse: bool) -> CapabilityManifest:
    """Build the read-only Databricks capability manifest.

    ``has_sql_warehouse`` is the one runtime-varying input this manifest needs (decision
    D6). Pass ``config.has_sql_warehouse`` (:class:`~qlabs_connector_databricks.config.
    DatabricksConfig`) for a real endpoint (or use :func:`manifest_for_config` directly),
    and an explicit ``bool`` in tests to exercise both shapes deterministically. Every
    other declaration here is a fixed fact about the Databricks REST/Unity Catalog API
    surface (RS-01 sections 1.2, 1.3, 4.1-4.3) and does not vary per endpoint.
    """
    tag_capability = FieldCapability.ro() if has_sql_warehouse else FieldCapability.na()

    data_product = EntityCapability(
        supported=True,
        identity_keys=list(_IDENTITY_KEYS),
        fields={
            # Schema `name` / `comment` (RS-01 section 1.2) — always readable.
            "name": FieldCapability.ro(),
            "description": FieldCapability.ro(),
            # No schema-level long-form doc distinct from `comment`. A Marketplace
            # listing's `readMe` is a different metadata surface belonging to a share/
            # listing, and shares/listings are deferred (decision item 1).
            "documentation": FieldCapability.na(),
            # Unity Catalog has no draft/active/deprecated/archived lifecycle on a
            # schema; there is nothing to read into the neutral `status` enum.
            "status": FieldCapability.na(),
            # Schema `owner` (RS-01 section 1.2) — read-only, mapped to a
            # single-element `Party` list.
            "owners": FieldCapability.ro(),
            # D6: SCHEMA_TAGS via INFORMATION_SCHEMA needs a SQL warehouse to read.
            "tags": tag_capability,
            # D1: the schema's tables/views ARE its datasets. Enumerating them is
            # exactly what makes the schema-as-data-product mapping readable; final
            # neutralId resolution against the IdentityMap is the engine's job.
            "dataset_refs": FieldCapability.ro(),
            # No native glossary at all (decision item 5).
            "glossary_term_refs": FieldCapability.na(),
            # No Unity Catalog concept of space/domain/collection placement — that is
            # a Qlik-space idea, and D1 confines the mapping to `catalog.schema` with
            # no separate "placement" surface.
            "placement": FieldCapability.na(),
        },
        supports_events=False,
    )

    dataset = EntityCapability(
        supported=True,
        identity_keys=list(_IDENTITY_KEYS),
        fields={
            # Table/view `name` / `comment` (RS-01 section 1.2).
            "name": FieldCapability.ro(),
            "description": FieldCapability.ro(),
            "owners": FieldCapability.ro(),
            # D6: TABLE_TAGS via INFORMATION_SCHEMA needs a SQL warehouse to read.
            "tags": tag_capability,
            # RS-03 section 8.2: Databricks realizes `classifications` through the
            # same tag surface ("RW (tags)"), so it inherits D6's conditionality
            # exactly rather than being declared unconditionally `ro`.
            "classifications": tag_capability,
            "glossary_term_refs": FieldCapability.na(),
            # `full_name` (derived, RS-01 section 4.3) is the readable physical
            # identity; it is never a write target even where a warehouse exists.
            "physical_ref": FieldCapability.ro(),
            # Derived from `table_type` / `data_source_format` (RS-01 sections 1.2,
            # 4.3) — both structural, both read-only, both feed this one enum.
            "asset_type": FieldCapability.ro(),
        },
        supports_events=False,
    )

    return CapabilityManifest(
        entities={
            EntityType.DATA_PRODUCT: data_product,
            EntityType.DATASET: dataset,
            # Declared `supported=False`, not omitted: Databricks has no native
            # glossary or category/domain concept at all (decision item 5). See the
            # module docstring for why this differs from Qlik's D5.
            EntityType.GLOSSARY_TERM: EntityCapability(supported=False),
            EntityType.CATEGORY: EntityCapability(supported=False),
        },
        # No ETag/revision on Unity Catalog objects; change detection is snapshot +
        # checksum comparison (RS-03 section 6), not a server-side concurrency token.
        concurrency=ConcurrencyMode.NONE,
    )


def manifest_for_config(config: DatabricksConfig) -> CapabilityManifest:
    """Build the manifest straight from a bound connector config.

    Equivalent to ``build_manifest(has_sql_warehouse=config.has_sql_warehouse)``. This is
    the entry point ``Connector.capabilities()`` should call once it has a config to read
    from — see this task's report for exactly where and how to wire it into
    ``__init__.py`` (out of this task's owned paths).
    """
    return build_manifest(has_sql_warehouse=config.has_sql_warehouse)
