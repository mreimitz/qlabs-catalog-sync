"""The capability-honesty check that matters most for a read-only source connector:
``create``, ``update`` and ``delete`` raise ``CapabilityError`` **without issuing a
request** -- for the entity types the base suite has no reason to try.

``suite.py``'s ``test_unsupported_entities_refuse_writes_with_capability_error`` proves
``create``/``delete`` refuse for entity types the manifest does not support at all
(``GLOSSARY_TERM``/``CATEGORY`` here). It never calls ``create``/``delete`` for a
*supported* entity type -- reasonable in general, since a connector that supports an
entity type for reads usually supports it for at least one write too. Snowflake is
exactly the edge case where that does not hold: ``DATA_PRODUCT`` and ``DATASET`` are both
``supported=True`` (for reads) while every one of their fields is ``ro``/``na``
(``manifest.py``: "every field declared below is ``ro`` or ``na``, never ``rw``"). Nothing
in the base suite proves ``create()``/``delete()`` refuse honestly for these two -- this
module closes exactly that gap, and is the v1 guardrail ("source connectors are read-only,
no ``create``/``update``/``delete``") expressed as an executable test rather than a
comment.

Verifiability, stated once here rather than per test: this connector has a single
transport. ``__init__.py`` sends everything through the SDK's httpx-based
``HttpEndpoint``, and ``auth.py`` uses ``snowflake-connector-python`` only for a local
public-key fingerprint computation -- there is no vendor client holding a socket. respx
sees every request this connector can make, so ``assert_no_http_calls`` here proves "no
request was sent", not merely "respx saw nothing" (the caveat ``harness.py``'s own module
docstring raises for connectors on a non-httpx transport does not apply). On top of that,
``__init__.py`` never overrides ``create``/``update``/``delete``, so each is the inherited
``Connector`` ABC default (``contract.py``) that raises as its first and only statement.
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

#: Both entity types this connector declares ``supported=True`` for (``manifest.py``) --
#: the ones the base suite's unsupported-entity check never exercises a create/delete
#: refusal for.
SUPPORTED_ENTITY_TYPES = (EntityType.DATA_PRODUCT, EntityType.DATASET)


def _synthetic_ref(entity_type: EntityType) -> IdentityRef:
    """A syntactically valid ref to an object that does not exist -- safe for the same
    reason ``suite.py``'s own ``_synthetic_ref`` is: the contract's capability guard runs
    before any existence lookup, so a connector that refuses correctly never even asks
    "does this exist?" for a write it was never going to honor."""
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=entity_type,
        native_key="WRITE_REFUSAL.SYNTHETIC_REF",
        tenant_id="WRITE-REFUSAL-SYNTHETIC-TENANT",
    )


@pytest.fixture
async def connector() -> AsyncIterator[Connector]:
    async with setup_connector() as connector:
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
    guarantee down for one concrete field (``name``) both entity types actually carry,
    readable on its own as "no write path exists"."""
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


async def test_the_manifest_declares_no_writable_field_anywhere(connector: Connector) -> None:
    """The declaration behind every refusal above: no entity type, supported or not,
    carries a single ``rw`` field, and none carries ``writable_via`` or
    ``allowed_update_paths``. Without this, the tests above would be compatible with a
    manifest that *promised* writes the connector then refused -- the dishonest
    combination the capability manifest exists to rule out."""
    manifest = connector.capabilities()
    for entity_type in EntityType:
        capability = manifest.entity_capability(entity_type)
        if capability is None:
            continue
        assert capability.allowed_update_paths is None, (
            f"{entity_type.value} declares update paths, but nothing here is updatable"
        )
        for name, field_capability in capability.fields.items():
            assert not field_capability.is_writable, (
                f"{entity_type.value}.{name} is declared writable, but Snowflake is a "
                "read-only source connector (v1 scope guardrail)"
            )
            assert field_capability.writable_via is None
