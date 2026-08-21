"""Snowflake capability manifest.

WP6 / T6.2. Declares this connector's capability honestly for the Snowflake object
kinds it can see, drawn from
``planning/Research/RS-05-snowflake-catalog-api/outputs/snowflake-catalog-api-reference.md``
(RS-05) sections 1.2 (the securable-object hierarchy), 2 (data products / listings), and
4 (writable-vs-read-only, identity keys): a Snowflake **schema** as
``EntityType.DATA_PRODUCT`` and a **table or view** inside that schema as
``EntityType.DATASET`` — mirroring the Databricks connector's schema-as-data-product
precedent (``qlabs_connector_databricks.manifest``) — plus, because Snowflake's own
"data product" vocabulary literally names the thing behind a **listing** (RS-05 section
2.1: "a data product is the thing attached to a listing"), ``DATA_PRODUCT`` also covers a
listing. One neutral entity type, two native shapes; see the identity-keys note below for
how that is expressed honestly rather than by inventing a second entity type the SDK's
``EntityType`` enum does not have.

**Read-only, everywhere.** This is a v1 source connector (agent guide "v1 scope
guardrails"): every field declared below is ``ro`` or ``na``, never ``rw``, and no field
carries ``writable_via`` or ``allowed_update_paths``. The inherited ``Connector``
write-method defaults (``create``/``update``/``delete``) already refuse with
``CapabilityError`` regardless of what this manifest says; declaring nothing writable is
what makes that refusal an honest reflection of the manifest rather than an accident of
what ``__init__.py`` happens not to override.

**Identity — the two DATA_PRODUCT shapes (RS-05 section 4.3):**

* A schema-shaped data product identifies by its fully-qualified name,
  ``DATABASE.SCHEMA`` (RS-05 section 1.2: "The fully-qualified name (FQN) of a
  schema-level object is ``DATABASE.SCHEMA.OBJECT``... This FQN is the primary
  matching/identity key").
* A listing-shaped data product identifies by its **listing global name** — "a stable,
  globally-unique identifier that is the best matching key for a listing across
  accounts/regions" (RS-05 section 2.4), returned as the ``global_name`` column of
  ``SHOW LISTINGS``.

``identity_keys = ["fully_qualified_name", "listing_global_name"]`` declares both,
deliberately kept to exactly these two rather than also carrying a schema-level numeric
id the way ``DATASET`` does below: the two shapes are mutually exclusive per instance (a
given ``DataProduct`` reflects a schema *or* a listing, never both at once), so this list
is "the identifying key(s) a Snowflake data product may carry", not "every key one
instance carries simultaneously" the way Databricks' ``["full_name", "object_id"]`` is for
one uniform shape. Which native fields actually get populated for which shape is a
mapping-time (T6.5) concern; this manifest only needs to declare that both identity
schemes exist and are honestly read-only.

Share identity (RS-05 section 2.1: shares are the access-control substrate a listing
wraps; section 4.3: share name / ``<provider_account>.<share_name>``) is deliberately
**not** carried as a third top-level identity key here: a share is the plumbing beneath a
listing, not a separately-synced object the engine would track on its own, and the
listing's stable ``global_name`` is what RS-05 section 2.4 calls out as "decisively" the
right cross-account matching key for the data product itself.

**Identity — DATASET (structural table/view):** ``identity_keys =
["fully_qualified_name", "object_id"]`` — the three-level qualified name
(``DATABASE.SCHEMA.OBJECT``, RS-05 section 1.2/4.3) plus the object's internal numeric id,
"useful for detecting renames" (RS-05 section 4.3: "Objects also have internal numeric IDs
in some ``ACCOUNT_USAGE`` views"). Mirrors the Databricks connector's identical
``full_name`` + ``object_id`` pairing for the same structural reason: names can be
renamed, ids cannot, so both are carried and the id is the safer join key.
TENANT-UNVERIFIED: RS-05 does not name the exact ``ACCOUNT_USAGE`` column per object kind
(``TABLES.TABLE_ID`` is the one explicitly documented; the equivalent for a view is not
spelled out), and that column's actual availability/latency needs live-tenant
confirmation — see this task's report.

**Concurrency:** ``ConcurrencyMode.NONE``. RS-05 documents no ETag or revision counter on
these Snowflake objects that a read-only connector could use for optimistic concurrency
(there is nothing to write anyway); change detection is snapshot + checksum comparison
against the last-read field envelope, exactly the Databricks connector's rationale for the
same setting, not a server-side token.

**Config-independence (unlike the Databricks manifest's D6):** ``build_manifest()`` takes
no arguments and this module does not import ``auth.py`` at all. Databricks gates its
``tags`` field on a configured SQL warehouse (decision D6) because an ordinary
``SELECT`` against ``INFORMATION_SCHEMA`` needs one. RS-05 section 3.4's tag-read surface
for Snowflake is different: ``SYSTEM$GET_TAG``, the ``INFORMATION_SCHEMA.TAG_REFERENCES``
*table function*, and ``ACCOUNT_USAGE.TAG_REFERENCES`` are the documented read paths, and
RS-05 does not identify any of them as requiring a running virtual warehouse the way a
plain ``SELECT`` over table data does (Snowflake generally serves ``SHOW``/table-function
metadata calls off the cloud-services layer without warehouse credits). Given that, and
given this task's own signature requirement (``build_manifest()`` with no parameters,
manifest independent of config), ``tags``/``classifications`` below are declared
unconditionally ``ro`` rather than gated the way Databricks gates them.
TENANT-UNVERIFIED: if a live tenant shows these specific calls do in fact require an
active warehouse in all cases, that would be a reason to revisit this — flagged in this
task's report rather than silently assumed.

**What has no manifest entry, and why that is still honest:**
``EntityCapability.fields`` (SDK ``manifest.py``) already treats a neutral field that is
simply absent from this dict exactly like ``na`` — so every field ``DataProduct`` and
``Dataset`` actually declare (RS-03 section 3) gets an explicit entry below rather than a
silent omission; see ``tests/manifest/test_declaration.py`` for the exhaustiveness check
that keeps this true as the neutral model evolves. Native lineage, audit/system fields
(``created_on``, ``last_altered``, row counts, bytes) and grants are excluded outright for
a stronger reason than omission: RS-05 section 4.2 marks them read-only *system* facts,
but none of them correspond to any attribute the neutral ``DataProduct``/``Dataset``
models declare, so there is no neutral field to hang a capability on in the first place.
``NeutralEntity.custom_attributes`` is likewise never given its own ``FieldCapability``
entry, matching every other manifest in this codebase — it is a free-form, always
round-tripped sidecar, not a field capability negotiation participates in.

**Glossary and semantic layer:** ``GLOSSARY_TERM`` and ``CATEGORY`` are declared
``supported=False``, not omitted — a considered "no" for *this* task, matching the
Databricks connector's pattern for telling a deliberate exclusion apart from an
oversight. Snowflake does have a genuine business-semantics analog — **Semantic Views**
(RS-05 section 5): a schema-level object carrying business-entity mappings, synonyms,
metric/dimension definitions, comments and tags, explicitly classified by Snowflake as
metadata and shareable through listings. That is a materially richer structure than the
neutral ``GlossaryTerm``/``Category`` pair (logical tables, relationships, facts,
dimensions and metrics, not a flat term list), and mapping it is out of this task's scope
(T6.1/T6.2 only cover auth and this manifest) — noted here so a future task designing
Snowflake glossary/semantic support starts from RS-05 section 5 rather than concluding
Snowflake has nothing to offer.

**Tags and comments (the DoD's explicit "tags/comments support" line):** RS-05 sections
1.3, 3.3 and 3.4 establish that both are read/write via SQL, fully documented,
account-wide readable through ``ACCOUNT_USAGE``. This manifest declares both `ro` for
every entity below they apply to — see ``description`` (comments) and ``tags``.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.manifest import (
    CapabilityManifest,
    ConcurrencyMode,
    EntityCapability,
    FieldCapability,
)
from qlabs_catalog_sync_sdk.models import EntityType

__all__ = ["build_manifest"]

#: Both identity schemes a Snowflake DATA_PRODUCT can carry (RS-05 section 4.3): a
#: schema's fully-qualified name, or a listing's global name. See the module docstring
#: for why these are declared together rather than each getting its own entity type.
_DATA_PRODUCT_IDENTITY_KEYS = ["fully_qualified_name", "listing_global_name"]

#: A structural table/view's identity (RS-05 section 4.3): the three-level qualified
#: name plus the object's stable internal id, mirroring the Databricks connector's
#: identical pairing for the identical structural reason (names rename, ids do not).
_DATASET_IDENTITY_KEYS = ["fully_qualified_name", "object_id"]


def build_manifest() -> CapabilityManifest:
    """Build the read-only Snowflake capability manifest.

    Takes no arguments and this module imports nothing from ``auth.py`` — every
    declaration below is a fixed fact about the Snowflake catalog/listing surface
    (RS-05 sections 1-4), not something that varies with a resolved connector config.
    ``__init__.py``'s ``Connector.capabilities()`` calls this directly and can do so
    before ``setup()`` has run.
    """
    data_product = EntityCapability(
        supported=True,
        identity_keys=list(_DATA_PRODUCT_IDENTITY_KEYS),
        fields={
            # Schema NAME / COMMENT, or a listing's title / description — both are
            # read/write via SQL for the schema shape (RS-05 3.3) and read-only
            # "operational state" outputs for the listing shape (RS-05 4.2); either
            # way this connector only ever reads.
            "name": FieldCapability.ro(),
            "description": FieldCapability.ro(),
            # No schema-level long-form doc distinct from `comment` (matching the
            # Databricks precedent) — but a listing's manifest carries a genuine
            # long-form Markdown description up to 7,500 chars (RS-05 2.2/3.6),
            # distinct from its short `subtitle`/title. Declared readable because the
            # listing shape genuinely has one, even though the schema shape does not.
            "documentation": FieldCapability.ro(),
            # RS-05 4.2: "Listing operational state (global name, publish status, ...)
            # — read-only outputs." A schema has no lifecycle at all (matching
            # Databricks); a listing's publish/draft/unpublish state is exactly what
            # this field is for on the listing shape.
            "status": FieldCapability.ro(),
            # Schema owning role (RS-05 1.3/4.2: "Ownership / grants — readable as
            # metadata... changed only through privilege operations"). Best-effort
            # metadata only, never an identity system (project guardrail).
            "owners": FieldCapability.ro(),
            # RS-05 3.4/4.1: tags are read (and write, but this connector is
            # read-only) via SYSTEM$GET_TAG / TAG_REFERENCES for any taggable object,
            # schemas included. See the module docstring for why this is
            # unconditional here, unlike Databricks' SQL-warehouse-gated D6.
            "tags": FieldCapability.ro(),
            # Schema shape: "the schema's tables/views ARE its datasets" (identical
            # Databricks precedent). Listing shape: RS-05 2.2's `data_dictionary`
            # section is exactly this — the listing's own enumeration of its member
            # database/schema/object entries. Both shapes genuinely support reading
            # this, so it is `ro` rather than shape-conditional.
            "dataset_refs": FieldCapability.ro(),
            # No native glossary at all for either shape — see the module docstring's
            # "Glossary and semantic layer" section for the semantic-views exception,
            # which is out of this task's scope.
            "glossary_term_refs": FieldCapability.na(),
            # No Snowflake analog for a Qlik-style space/workspace placement. A
            # listing's `categories` (RS-05 2.2/3.6: "a single value from a fixed
            # set") is closer to classification than placement and has no neutral
            # field of its own — it round-trips through `custom_attributes` instead.
            "placement": FieldCapability.na(),
        },
        supports_events=False,
    )

    dataset = EntityCapability(
        supported=True,
        identity_keys=list(_DATASET_IDENTITY_KEYS),
        fields={
            # Table/view NAME / COMMENT (RS-05 1.2/1.3, 3.3).
            "name": FieldCapability.ro(),
            "description": FieldCapability.ro(),
            "owners": FieldCapability.ro(),
            "tags": FieldCapability.ro(),
            # RS-05 1.3/4.2: system classification tags (SEMANTIC_CATEGORY /
            # PRIVACY_CATEGORY) are column-level but readable and explicitly called
            # out as readable metadata; aggregated onto the table's neutral
            # `classifications` list, unconditionally `ro` for the same reason `tags`
            # is (see module docstring).
            "classifications": FieldCapability.ro(),
            "glossary_term_refs": FieldCapability.na(),
            # The fully-qualified name is the readable physical identity — never a
            # write target even though it is also carried in `identity_keys`.
            "physical_ref": FieldCapability.ro(),
            # Derived from Snowflake's table kind (standard/transient/external/
            # dynamic/event/Iceberg) or view kind (standard/materialized/secure/
            # semantic) — RS-05 1.2 — both structural, both read-only.
            "asset_type": FieldCapability.ro(),
        },
        supports_events=False,
    )

    return CapabilityManifest(
        entities={
            EntityType.DATA_PRODUCT: data_product,
            EntityType.DATASET: dataset,
            # Declared `supported=False`, not omitted — see the module docstring's
            # "Glossary and semantic layer" section.
            EntityType.GLOSSARY_TERM: EntityCapability(supported=False),
            EntityType.CATEGORY: EntityCapability(supported=False),
        },
        # No ETag/revision on these Snowflake objects; change detection is snapshot +
        # checksum comparison (T6.3), not a server-side concurrency token.
        concurrency=ConcurrencyMode.NONE,
    )
