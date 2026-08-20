"""The capability-honesty check the task brief names as what matters most here:
attempting ``create``, ``update`` and ``delete`` raises ``CapabilityError`` **without
issuing a request** -- exercised for the entity types the base suite has no reason to
try.

``suite.py``'s ``test_unsupported_entities_refuse_writes_with_capability_error`` proves
``create``/``delete`` refuse for entity types the manifest does not support *at all*
(``GLOSSARY_TERM``/``CATEGORY`` here). It never calls ``create``/``delete`` for an entity
type the manifest *does* support -- reasonably so in general, since a connector that
supports an entity type for reads usually supports it for at least one write too, and
the suite already covers `update` exhaustively for every ro/na field regardless. But
Databricks is exactly the edge case where that assumption does not hold: ``DATA_PRODUCT``
and ``DATASET`` are both declared ``supported=True`` (for reads) while every one of their
fields is ``ro``/``na`` (``manifest.py``'s own docstring: "every field declared below is
``ro`` or ``na``, never ``rw```"). Nothing in the base suite proves ``create()``/
``delete()`` refuse honestly for *these* two entity types specifically -- this module
closes exactly that gap.

Verifiability caveat, stated once here rather than repeated per test: Databricks never
overrides ``create``/``update``/``delete`` (``__init__.py``'s own comment) and the
inherited ``Connector`` ABC defaults (``contract.py``) unconditionally raise
``CapabilityError`` as their first and only statement, touching neither ``self._http``
(SDK ``HttpEndpoint``, httpx-based, respx-visible) nor ``self._client``
(``databricks-sdk``'s ``WorkspaceClient``, ``requests``-based, respx-**blind** --
``harness.py``'s own module docstring). Because that code path cannot reach either
client, ``assert_no_http_calls`` here is a sound proof of "no request on any transport",
not just "respx saw nothing" -- see ``test_databricks_conformance_suite.py``'s module
docstring for the same point made about the base suite's own checks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from qlabs_catalog_sync_sdk.conformance.harness import assert_no_http_calls
from qlabs_catalog_sync_sdk.conformance.samples import sample_entity, sample_value
from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.envelope import to_json_value
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.models import (
    EntityType,
    FieldChange,
    FieldDiff,
    FieldUpdateMode,
    IdentityRef,
)

from .conftest import ENDPOINT, setup_connector

#: Both entity types this connector actually declares ``supported=True`` for
#: (``manifest.py``) -- the ones the base suite's unsupported-entity check never
#: exercises a create/delete refusal for.
SUPPORTED_ENTITY_TYPES = (EntityType.DATA_PRODUCT, EntityType.DATASET)


def _synthetic_ref(entity_type: EntityType) -> IdentityRef:
    """A syntactically valid ref to an object that does not exist -- safe here for the
    same reason ``suite.py``'s own ``_synthetic_ref`` is: the contract's guards run
    before any existence lookup, so a connector that refuses correctly never even asks
    "does this exist?" for a write it was never going to honor."""
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=entity_type,
        native_key="write-refusal-synthetic-ref",
        tenant_id="write-refusal-synthetic-tenant",
    )


@pytest.fixture
async def connector() -> AsyncIterator[Connector]:
    async with setup_connector(sql_warehouse_id="warehouse-conformance-1") as connector:
        yield connector


@pytest.mark.parametrize("entity_type", SUPPORTED_ENTITY_TYPES)
async def test_create_on_a_supported_entity_type_refuses_without_a_request(
    connector: Connector, entity_type: EntityType
) -> None:
    with pytest.raises(CapabilityError), assert_no_http_calls():
        await connector.create(sample_entity(entity_type))


@pytest.mark.parametrize("entity_type", SUPPORTED_ENTITY_TYPES)
async def test_delete_on_a_supported_entity_type_refuses_without_a_request(
    connector: Connector, entity_type: EntityType
) -> None:
    with pytest.raises(CapabilityError), assert_no_http_calls():
        await connector.delete(_synthetic_ref(entity_type))


@pytest.mark.parametrize("entity_type", SUPPORTED_ENTITY_TYPES)
async def test_update_on_a_supported_entity_type_refuses_without_a_request(
    connector: Connector, entity_type: EntityType
) -> None:
    """Belt-and-suspenders alongside ``suite.py``'s own
    ``test_writing_a_ro_or_na_field_raises_capability_error``, which already proves this
    for every declared ro/na field on every supported entity type -- this pins the same
    guarantee down for one concrete field (``name``) every entity type here actually
    carries, read as a standalone "no write path exists" statement."""
    diff = FieldDiff(
        entity_type=entity_type,
        changes=[
            FieldChange(
                field="name",
                mode=FieldUpdateMode.PATCH,
                value=to_json_value(sample_value(entity_type, "name", variant=1)),
            )
        ],
        expected_revision=None,
    )
    with pytest.raises(CapabilityError), assert_no_http_calls():
        await connector.update(_synthetic_ref(entity_type), diff)
