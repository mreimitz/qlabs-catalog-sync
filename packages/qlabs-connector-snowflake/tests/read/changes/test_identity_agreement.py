"""The property the engine's whole poll-then-read loop rests on: the ``IdentityRef`` the
change feed emits for an object is *exactly* the one ``read()`` builds for that same
object, and ``read()`` can resolve it.

The engine calls ``list_changed()`` to get ``ChangeRef``s and then ``read(ref)`` on each.
If the two halves of ``read.py`` ever disagreed about what a Snowflake object's identity is
-- which native key, which tenant id, which secondary keys -- the engine could not connect
its own poll output to its own read input, and every candidate would come back ``NotFound``.

Both halves live in one module precisely so they cannot drift, but "cannot drift" is a
claim about the code as it stands, not a guarantee about the code as it will be edited.
These tests pin it: they compare against the identity builders the read path itself uses
(:func:`build_dataset`, :func:`build_schema_data_product`,
:func:`build_listing_data_product`), and then take one ``ChangeRef`` all the way through
:func:`read_entity` over the same mocked endpoint.

The keys asserted here are exactly the ones ``manifest.py`` declares -- FQN for both
entity types, ``object_id`` for ``DATASET`` only, the listing's local name for a listing --
and nothing else. An invented key would resolve fine and still be wrong: the manifest is
what the engine negotiates against.
"""

from __future__ import annotations

from typing import Any

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark
from qlabs_catalog_sync_sdk.models import Dataset
from qlabs_connector_snowflake.manifest import build_manifest
from qlabs_connector_snowflake.read import (
    StatementClient,
    build_dataset,
    build_listing_data_product,
    build_schema_data_product,
    read_entity,
)

from ..conftest import (
    COLUMNS_COLUMNS,
    ENDPOINT,
    LISTING_COLUMNS,
    TABLES_COLUMNS,
    TAG_REFERENCE_COLUMNS,
    TENANT_ID,
    StatementRouter,
    column_row,
    listing_row,
    tag_reference_row,
)
from ..conftest import table_row as information_schema_table_row
from .conftest import (
    ACCOUNT_USAGE_SCHEMATA_COLUMNS,
    ACCOUNT_USAGE_TABLES_COLUMNS,
    NOW_1,
    poll,
    schema_row,
    set_listings,
    set_now,
    set_schemata,
    set_tables,
    table_row,
)


def _as_mapping(columns: tuple[str, ...], row: list[Any]) -> dict[str, Any]:
    return dict(zip(columns, row, strict=True))


async def test_a_dataset_change_ref_matches_the_read_paths_own_identity(
    client: StatementClient, router: StatementRouter
) -> None:
    row = table_row("ORDERS", table_id="4242")
    set_now(router, NOW_1)
    set_tables(router, [row])

    result = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))

    expected = build_dataset(
        _as_mapping(ACCOUNT_USAGE_TABLES_COLUMNS, row),
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
        object_id="4242",
    ).identities[0]
    assert len(result.changes) == 1
    assert result.changes[0].ref == expected


async def test_a_schema_change_ref_matches_the_read_paths_own_identity(
    client: StatementClient, router: StatementRouter
) -> None:
    row = schema_row("PUBLIC", schema_id="2001")
    set_now(router, NOW_1)
    set_schemata(router, [row])
    set_listings(router, [])

    result = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )

    expected = build_schema_data_product(
        _as_mapping(ACCOUNT_USAGE_SCHEMATA_COLUMNS, row),
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
    ).identities[0]
    schema_change = next(c for c in result.changes if c.ref.native_key == "SALES_DB.PUBLIC")
    assert schema_change.ref == expected


async def test_a_listing_change_ref_matches_the_read_paths_own_identity(
    client: StatementClient, router: StatementRouter
) -> None:
    row = listing_row(name="SALES_DAILY", global_name="GZTS1")
    set_now(router, NOW_1)
    set_schemata(router, [])
    set_listings(router, [row])

    result = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )

    expected = build_listing_data_product(
        _as_mapping(LISTING_COLUMNS, row),
        endpoint=ENDPOINT,
        tenant_id=TENANT_ID,
    ).identities[0]
    assert len(result.changes) == 1
    assert result.changes[0].ref == expected


async def test_the_keys_emitted_are_the_keys_the_manifest_declares(
    client: StatementClient, router: StatementRouter
) -> None:
    """``DATASET`` declares ``object_id``; ``DATA_PRODUCT`` declares no id key at all. The
    change feed must not invent one for either, however convenient it would be."""
    manifest = build_manifest()
    assert "object_id" in (manifest.entities[EntityType.DATASET].identity_keys)
    assert "object_id" not in (manifest.entities[EntityType.DATA_PRODUCT].identity_keys)

    set_now(router, NOW_1, NOW_1)
    set_tables(router, [table_row("ORDERS", table_id="1")])
    set_schemata(router, [schema_row("PUBLIC", schema_id="2001")])
    set_listings(router, [])

    datasets = await poll(
        client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET)
    )
    products = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )

    assert datasets.changes[0].ref.secondary_keys == {"object_id": "1"}
    assert products.changes[0].ref.secondary_keys == {}


async def test_a_change_ref_can_be_read_back_through_read_entity(
    client: StatementClient, router: StatementRouter
) -> None:
    """The end-to-end proof, not just structural equality: take a ``ChangeRef`` this poll
    produced and hand it straight to ``read()``, exactly as the engine would."""
    set_now(router, NOW_1)
    set_tables(router, [table_row("ORDERS", table_id="4242")])
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [information_schema_table_row()])
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [column_row()])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [tag_reference_row()])

    result = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    entity = await read_entity(client, result.changes[0].ref)

    assert isinstance(entity, Dataset)
    assert entity.name == "ORDERS"
    assert entity.identities[0] == result.changes[0].ref
