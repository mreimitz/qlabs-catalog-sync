"""Pins the property that was broken: for the same raw UC payload, the ``IdentityRef``
this module's ``list_changed`` emits is *exactly* the one ``read.py``'s own identity
builders produce.

The engine calls ``list_changed()`` to get ``ChangeRef``s and then ``read(ref)`` on each
of them, so if the two modules ever disagreed about what a Unity Catalog object's
identity is — which native key, which tenant id, which secondary keys — the engine could
not connect its own poll output to its own read input. ``changes.py`` closes this by
construction: it calls ``read.py``'s ``build_schema_identity_ref``/
``build_table_identity_ref`` directly rather than re-deriving the same two decisions
(see ``changes.py``'s module docstring), so this test is less "does the logic happen to
agree" and more "prove the wiring that makes agreement automatic is actually in place" —
it would fail immediately if a future edit ever went back to hand-building a ref here.
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_databricks.changes import list_changed
from qlabs_connector_databricks.read import build_schema_identity_ref, build_table_identity_ref

from .conftest import (
    CATALOGS_PATH,
    ENDPOINT,
    SCHEMAS_PATH,
    TABLES_PATH,
    catalog,
    mock_single_page,
    schema,
    table,
)


async def test_schema_change_ref_identity_matches_read_py(respx_mock, http: HttpEndpoint) -> None:
    raw_schema = schema("main", "sales", schema_id="sch-42")

    mock_single_page(
        respx_mock, CATALOGS_PATH, params={}, items_key="catalogs", items=[catalog("main")]
    )
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[raw_schema],
    )
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[],
    )

    result = await list_changed(
        http,
        EntityType.DATA_PRODUCT,
        Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        endpoint=ENDPOINT,
    )

    assert len(result.changes) == 1
    expected_ref = build_schema_identity_ref(raw_schema, endpoint=ENDPOINT)
    assert result.changes[0].ref == expected_ref


async def test_table_change_ref_identity_matches_read_py(respx_mock, http: HttpEndpoint) -> None:
    raw_table = table("main", "sales", "orders", table_id="tbl-42")

    mock_single_page(
        respx_mock, CATALOGS_PATH, params={}, items_key="catalogs", items=[catalog("main")]
    )
    mock_single_page(
        respx_mock,
        SCHEMAS_PATH,
        params={"catalog_name": "main"},
        items_key="schemas",
        items=[schema("main", "sales")],
    )
    mock_single_page(
        respx_mock,
        TABLES_PATH,
        params={"catalog_name": "main", "schema_name": "sales"},
        items_key="tables",
        items=[raw_table],
    )

    result = await list_changed(
        http,
        EntityType.DATASET,
        Watermark.initial(ENDPOINT, EntityType.DATASET),
        endpoint=ENDPOINT,
    )

    assert len(result.changes) == 1
    expected_ref = build_table_identity_ref(raw_table, endpoint=ENDPOINT)
    assert result.changes[0].ref == expected_ref
