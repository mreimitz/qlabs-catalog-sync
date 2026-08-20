"""A read-honesty check the base ``ConnectorConformanceSuite`` has no generic mechanism
for: does ``read()`` actually deliver what the manifest promises for a field it declares
``ro``? The suite's round-trip/idempotency checks (``suite.py``) only ever exercise
*writable* fields, and skip entirely for this connector -- every field here is ``ro``/
``na`` (see ``test_databricks_conformance_suite.py``'s module docstring). Nothing in the
base kit checks a read-only field's promise against what ``read()`` actually returns.

**Finding, stated up front: it does not, for exactly two fields.** ``manifest.py``
declares ``Dataset.tags``, ``Dataset.classifications`` and ``DataProduct.tags`` ``ro``
whenever ``DatabricksConfig.has_sql_warehouse`` is true (decision D6) -- and
``FieldCapabilityMode.RO``'s own docstring (SDK ``manifest.py``) is explicit about what
that promises: "the endpoint can express the field but only ever returns it".
``sql_tags.py`` (T4.7) implements the read path that would deliver it
(``INFORMATION_SCHEMA.*_TAGS`` over the Statement Execution API), but nothing calls it:

* ``read.py`` (T4.4)'s own module docstring says plainly "``tags`` stays ``[]`` here"
  and names ``sql_tags.py`` as **T4.7**'s seam -- a *later* task's job to wire in.
* ``mapping.py`` (T4.5)'s own module docstring says the same: "This module does not
  import it and does not set either field."
* ``sql_tags.py``'s own module docstring calls the wiring "out of this module's owned
  paths -- see this task's report".

No WP4 task's ``owns_paths`` covers ``read.py``/``mapping.py``/``__init__.py`` *and*
``sql_tags.py`` together (checked directly against ``planning/tools/agent-plan/
tasks.json``: T4.1-T4.5 and T4.7 are all ``done``, none of their owned paths overlap this
way), and T4.6 (this task) owns only ``tests/conformance/`` + ``tests/cassettes/`` -- so
wiring the two together was never anyone's job. ``Connector.read()`` (``__init__.py``)
calls ``read.read_entity()`` alone, which never invokes ``sql_tags.read_catalog_tags``/
``read_tags_for_catalogs`` no matter how ``DatabricksConfig.sql_warehouse_id`` is set.

The tests below pin this down with a **positive** proof, not just an absence: a
Statement Execution API route that *would* answer with real tag rows, asserted to
actually receive the call and to have its rows reach the entity. They were written
against a real defect — ``read()`` never called ``sql_tags`` at all — and are kept as the
standing guarantee that the manifest's ``ro`` promise for ``tags`` is honored rather than
merely declared.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.manifest import FieldCapabilityMode
from qlabs_catalog_sync_sdk.models import EntityType, IdentityRef

from .conftest import (
    ENDPOINT,
    METASTORE_ID,
    STATEMENTS_URL,
    make_schema,
    make_table,
    mock_get_table,
    mock_schema_list,
    mock_table_list,
    setup_connector,
)

#: A raw ``INFORMATION_SCHEMA.TABLE_TAGS`` row shape (RS-01 section 1.3 names the table,
#: not its exact columns -- ``sql_tags.py``'s own docstring flags the column order as
#: TENANT_UNVERIFIED assumption 2; this mirrors that module's own selected column order:
#: catalog_name, schema_name, table_name, tag_name, tag_value).
_REAL_TABLE_TAG_ROWS = [["prod", "sales", "orders", "pii", "true"]]
_REAL_SCHEMA_TAG_ROWS = [["prod", "sales", "domain", "sales"]]


def _statement_response(rows: list[list[str]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "statement_id": "stmt-conformance-1",
            "status": {"state": "SUCCEEDED"},
            "manifest": {"format": "JSON_ARRAY"},
            "result": {"data_array": rows},
        },
    )


def _tag_statement_responder(request: httpx.Request) -> httpx.Response:
    """Answer each tag statement with its own table's column set.

    A single canned response for both statements would feed four-column SCHEMA_TAGS rows
    to the five-column TABLE_TAGS parser, which the connector now rejects outright — so a
    shared mock would be testing an impossible workspace rather than a real one.
    """
    statement = request.content.decode()
    rows = _REAL_TABLE_TAG_ROWS if "TABLE_TAGS" in statement else _REAL_SCHEMA_TAG_ROWS
    return _statement_response(rows)


@pytest.fixture
async def connector_with_warehouse() -> AsyncIterator[Connector]:
    async with setup_connector(sql_warehouse_id="warehouse-conformance-1") as connector:
        yield connector


async def test_manifest_declares_dataset_tags_and_classifications_ro_with_a_warehouse(
    connector_with_warehouse: Connector,
) -> None:
    """Sanity check the premise before the xfail tests below rely on it: this is a real
    'ro' promise per decision D6, not a hypothetical."""
    manifest = connector_with_warehouse.capabilities()
    capability = manifest.entity_capability(EntityType.DATASET)
    assert capability is not None
    assert capability.fields["tags"].mode is FieldCapabilityMode.RO
    assert capability.fields["classifications"].mode is FieldCapabilityMode.RO
    data_product_capability = manifest.entity_capability(EntityType.DATA_PRODUCT)
    assert data_product_capability is not None
    assert data_product_capability.fields["tags"].mode is FieldCapabilityMode.RO


async def test_read_dataset_without_a_warehouse_correctly_makes_no_statement_calls() -> None:
    """The other D6 branch is honest: 'na' promises nothing, and indeed nothing is
    fetched. Included to show the gap below is specific to the 'ro'-with-warehouse
    promise, not a blanket "tags never work" statement."""
    table = make_table("prod", "sales", "orders")
    full_name = table["full_name"]
    ref = IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key=table["table_id"],
        tenant_id=METASTORE_ID,
        secondary_keys={"full_name": full_name},
    )
    async with setup_connector(sql_warehouse_id=None) as connector:
        manifest = connector.capabilities()
        capability = manifest.entity_capability(EntityType.DATASET)
        assert capability is not None
        assert capability.fields["tags"].mode is FieldCapabilityMode.NA

        with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
            mock_get_table(router, full_name=full_name, table=table)
            statement_route = router.post(STATEMENTS_URL).mock(
                side_effect=_tag_statement_responder
            )
            dataset = await connector.read(ref)

        assert statement_route.call_count == 0
        assert dataset.tags == []
        assert dataset.classifications == []


async def test_read_dataset_delivers_the_tags_its_ro_manifest_promises(
    connector_with_warehouse: Connector,
) -> None:
    table = make_table("prod", "sales", "orders")
    full_name = table["full_name"]
    ref = IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key=table["table_id"],
        tenant_id=METASTORE_ID,
        secondary_keys={"full_name": full_name},
    )
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        mock_get_table(router, full_name=full_name, table=table)
        # This route WOULD hand back a real "pii" tag if the connector ever asked --
        # proving the gap is "never called", not "called and got nothing back".
        statement_route = router.post(STATEMENTS_URL).mock(
            side_effect=_tag_statement_responder
        )
        dataset = await connector_with_warehouse.read(ref)

    assert statement_route.call_count > 0, (
        "read() never called the Statement Execution API at all, even though "
        "has_sql_warehouse=True and the manifest declares 'tags'/'classifications' "
        "'ro' -- read() cannot be delivering what it promises without ever asking"
    )
    assert [tag.key for tag in dataset.tags] == ["pii"]
    assert dataset.classifications == ["pii"]
    assert "tags" in dataset.field_envelopes


async def test_read_schema_delivers_the_tags_its_ro_manifest_promises(
    connector_with_warehouse: Connector,
) -> None:
    schema = make_schema("prod", "sales")
    full_name = schema["full_name"]
    ref = IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=schema["schema_id"],
        tenant_id=METASTORE_ID,
        secondary_keys={"full_name": full_name},
    )
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        mock_schema_list(router, catalog_name="prod", schemas=[schema])
        mock_table_list(router, catalog_name="prod", schema_name="sales", tables=[])
        statement_route = router.post(STATEMENTS_URL).mock(
            side_effect=_tag_statement_responder
        )
        data_product = await connector_with_warehouse.read(ref)

    assert statement_route.call_count > 0, (
        "read() never called the Statement Execution API at all for the schema-level "
        "'tags' field either, despite the same 'ro' promise"
    )
    assert [tag.key for tag in data_product.tags] == ["domain"]
