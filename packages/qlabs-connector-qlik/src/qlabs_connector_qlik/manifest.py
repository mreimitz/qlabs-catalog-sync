"""Qlik capability manifest — what the sole v1 write connector can honestly do.

WP3 / T3.2. Builds the concrete ``manifest.CapabilityManifest`` (T1.3) for the Qlik
connector via :func:`qlik_capability_manifest`. The engine plans every write strictly
from what this module declares, so every entry here is a deliberate, researched
choice rather than a guess:

* ``DATA_PRODUCT`` is the only entity type this connector ever writes. Its ``rw``
  fields are exactly the ones the Qlik data-governance API can actually take a value
  for, at create or through the closed PATCH path enum below.
* ``DATASET`` is declared **read-only in this connector's hands** — decision D2 (see
  ``planning/Roadmap/completed/RM-01-one-way-sync-mvp/decision-databricks-to-qlik-mvp.md``): the
  connector never creates or updates a Qlik dataset, it only *resolves* ``datasetIds``
  against datasets that already exist in the target space (IdentityMap first, then a
  name match within the space). Qlik's Items API can technically express writes to
  some dataset fields (RS-03 section 8.2), but this connector implements no dataset
  write path at all, so declaring any dataset field ``rw`` would be a manifest the
  write path cannot honor — exactly the dishonest-manifest failure mode this type
  exists to prevent.
* ``GLOSSARY_TERM`` and ``CATEGORY`` are declared **unsupported** — decision D5:
  Databricks, the only v1 source, has no glossary to sync one from. Both are declared,
  not simply omitted, so a reader can tell this was a choice and not an oversight; the
  Qlik glossary write path is Track B (RM-05), not this build.

Wire facts encoded here, from RS-02's
``Research/RS-02-qlik-catalog-api/notes/qlik-two-way-sync-readiness.md``:

* section 2 — the data-product PATCH endpoint accepts ``op: "replace"`` only, against
  the closed eight-path enum in :data:`QLIK_DATA_PRODUCT_PATCH_PATHS`, capped at 8
  operations per request (one per path); every array path (``datasetIds``,
  ``glossaryIds``, ``tags``, ``keyContacts``) is a full replace, never a delta — hence
  ``partial_update=False`` on the neutral fields backing them.
* section 2 — the POST-create body's writable fields (``name``, ``description``,
  ``readMe``, ``spaceId``, ``tags``, ``datasetIds``, ``apiConsumableDatasetIds``,
  ``glossaryIds``, ``keyContacts``) versus what the API only ever returns (``id``,
  ``qri``, ``ownerId``, ``tenantId``, computed ``quality``,
  ``activated``/``activatedAt``/``activatedOn``, ``pendingChangesCount``, audit
  timestamps, lineage, trust scores) — none of the latter appears as ``rw`` below.
* section 1 — Qlik publishes no webhook or audit-event coverage for items, datasets,
  data products or glossaries, so ``supports_events`` is ``False`` on every entity
  here; the engine always polls (``list_changed``), never subscribes.
* section 3 — identity durability: a data product's ``id`` and ``qri`` are equivalent
  join keys (the ``id`` is the value embedded inside the ``qri``); a dataset's durable
  key is ``secureQri`` (the forward-looking one — the legacy ``qri`` is being
  deprecated for datasets), with ``id``/``resourceId`` as secondary native keys.

Neutral field name -> Qlik wire path, per RS-03 section 8.1/8.2, is deliberately not
recorded here beyond the comments below: :attr:`EntityCapability.allowed_update_paths`
is documented as the endpoint's *native* wire paths, not neutral field names, and the
connector (``write.py``, T3.4/T3.5) owns that translation, not this manifest.

TENANT_UNVERIFIED candidate (flag for T8.6): RS-02 section 1 documents an optional
``if-match`` ETag header for glossary/category/term writes but does not document
whether the Data Products PATCH endpoint honors ETags at all — "concurrency control
there relies on the changelog rather than ETags" per that note.
``concurrency=ConcurrencyMode.ETAG`` is still the right declaration (glossary writes
are gated behind D5 anyway, and sending ``if-match`` on a product PATCH is harmless if
ignored), but whether it actually prevents a lost update on a live tenant is unproven
and needs a tenant probe before it can be trusted.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.manifest import (
    CapabilityManifest,
    ConcurrencyMode,
    EntityCapability,
    FieldCapability,
)
from qlabs_catalog_sync_sdk.models import EntityType

__all__ = ["QLIK_DATA_PRODUCT_PATCH_PATHS", "qlik_capability_manifest"]


#: The exact eight JSON Pointer paths Qlik's data-product PATCH endpoint accepts —
#: ``op: "replace"`` only, closed enum, max 8 operations per request (RS-02
#: qlik-two-way-sync-readiness.md section 2). ``/apiConsumableDatasetIds`` has no
#: neutral-model counterpart (``models.DataProduct`` declares no such attribute), so it
#: appears here — because the *wire* accepts it as a PATCH target — without a matching
#: entry in either entity's ``fields`` below; the connector currently has no neutral
#: source to plan a write to it from.
QLIK_DATA_PRODUCT_PATCH_PATHS: list[str] = [
    "/name",
    "/description",
    "/datasetIds",
    "/glossaryIds",
    "/readMe",
    "/keyContacts",
    "/tags",
    "/apiConsumableDatasetIds",
]


def _data_product_capability() -> EntityCapability:
    """DATA_PRODUCT: the one entity type this connector actually writes.

    Identity: ``id``/``qri`` are equivalent join keys (RS-02 section 3) since the
    ``id`` is embedded inside the ``qri``.
    """
    return EntityCapability(
        supported=True,
        identity_keys=["id", "qri"],
        fields={
            # -- rw: plain-string fields, writable at create and via a single PATCH
            #    replace (-> /name, /description, /readMe respectively). ------------
            "name": FieldCapability.rw(writable_via="rest-patch"),
            "description": FieldCapability.rw(writable_via="rest-patch"),
            # -> readMe. Qlik has no plain-vs-markdown concept, so the neutral
            # TextField.format cannot survive the round trip; the text itself does.
            "documentation": FieldCapability.rw(
                writable_via="rest-patch", normalized_by_target=True
            ),
            # -- ro: activation is opt-in and off in v1 ------------------------------
            # Neutral `active` maps to Qlik activation via the activate/deactivate
            # actions (RS-02 section 2), never a PATCH path — Qlik has no `/status`
            # path at all. Decision D7 makes activation opt-in per pair and off by
            # default, and nothing in v1 enables it (the connector's destructive-action
            # opt-in is empty by construction), so this endpoint genuinely cannot write
            # status today. Declaring it `rw` would be the manifest promising a write
            # the engine would then plan and the connector would refuse.
            "status": FieldCapability.ro(),
            # -- rw: arrays, full-replace only — Qlik never accepts a delta here
            #    (RS-02 section 2: "Array paths are full-replace"). -------------------
            # -> keyContacts. Qlik stores only {userId, role}: the email that resolved
            # the contact (decision D3) and the display name are not kept, so a read-back
            # carries the Qlik user id alone.
            "owners": FieldCapability.rw(
                writable_via="rest-patch", partial_update=False, normalized_by_target=True
            ),  # -> keyContacts
            # Qlik tags are a flat string[]; the neutral Tag is key/value, flattened
            # to "key=value" on write. Reading back cannot recover the split without
            # inventing structure in any tag a human typed with an "=" in it.
            "tags": FieldCapability.rw(
                writable_via="rest-patch", partial_update=False, normalized_by_target=True
            ),
            "dataset_refs": FieldCapability.rw(
                writable_via="rest-patch", partial_update=False
            ),  # -> datasetIds
            # -- na: glossary is out of the MVP (decision D5) ------------------------
            # Qlik's wire *can* express /glossaryIds (it is one of the 8 PATCH paths
            # above); this connector never has anything to put in it in v1, because
            # Databricks -- the only v1 source -- has no glossary. `na`, not merely
            # absent, so the omission reads as the deliberate D5 choice it is.
            "glossary_term_refs": FieldCapability.na(),  # -> glossaryIds
            # -- ro: `spaceId` is writable at create and via the separate `move`
            #    lifecycle action (which needs delete-in-source + create-in-target
            #    permission), but it is not one of the 8 PATCH paths above, so the
            #    field-level diff writer this manifest governs cannot reach it. -----
            "placement": FieldCapability.ro(),  # -> spaceId
        },
        # RS-02 section 1: Qlik publishes no webhooks/audit events for data products.
        supports_events=False,
        allowed_update_paths=list(QLIK_DATA_PRODUCT_PATCH_PATHS),
        max_update_operations=8,
    )


def _dataset_capability() -> EntityCapability:
    """DATASET: read-only in this connector's hands (decision D2).

    Identity: ``secureQri`` is the forward-looking durable key (the legacy ``qri`` is
    being deprecated for datasets); ``id``/``resourceId`` are stable secondary native
    keys the Items API also exposes (RS-02 section 3).
    """
    return EntityCapability(
        supported=True,
        identity_keys=["secure_qri", "id", "resource_id"],
        fields={
            "name": FieldCapability.ro(),
            "description": FieldCapability.ro(),
            "owners": FieldCapability.ro(),
            "tags": FieldCapability.ro(),
            "classifications": FieldCapability.ro(),
            # Glossary is out of the MVP (decision D5) here too.
            "glossary_term_refs": FieldCapability.na(),
            "physical_ref": FieldCapability.ro(),  # -> secureQri
            "asset_type": FieldCapability.ro(),  # -> resourceType
        },
        # RS-02 section 1: Qlik publishes no webhooks/audit events for datasets/items.
        supports_events=False,
    )


def qlik_capability_manifest() -> CapabilityManifest:
    """Build the Qlik connector's capability manifest.

    Called from ``Connector.capabilities()`` (``__init__.py``) — see this package's
    ``__init__.py`` module docstring for the exact wiring the orchestrator applies.
    Takes no arguments: unlike Databricks' D6 SQL-warehouse-gated ``tags``, nothing in
    the Qlik manifest varies with runtime config, so the same manifest is correct for
    every configured Qlik endpoint.
    """
    return CapabilityManifest(
        entities={
            EntityType.DATA_PRODUCT: _data_product_capability(),
            EntityType.DATASET: _dataset_capability(),
            # Decision D5: glossary is out of the MVP. Declared unsupported, not
            # omitted, so a reader can tell this was a choice, not an oversight — the
            # Qlik glossary write path is Track B (RM-05), not this build.
            EntityType.GLOSSARY_TERM: EntityCapability(supported=False),
            EntityType.CATEGORY: EntityCapability(supported=False),
        },
        concurrency=ConcurrencyMode.ETAG,
    )
