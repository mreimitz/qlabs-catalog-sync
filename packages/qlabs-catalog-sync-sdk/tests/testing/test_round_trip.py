"""In-memory store with real semantics: create/read round-trips, update is reflected on
re-read, an unknown ref 404s, delete removes, and — the property every engine
idempotency test leans on — re-applying an unchanged diff is a checksum-computed no-op.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.contract import ChangeKind, Watermark, WriteOutcome
from qlabs_catalog_sync_sdk.exceptions import ConflictError, NotFound
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    EntityType,
    FieldChange,
    FieldDiff,
    IdentityRef,
    TextField,
)
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_create_then_read_round_trips(target: FakeConnector) -> None:
    created = await target.create(
        DataProduct(name="Retail Sales", description=TextField.plain("Curated retail data."))
    )
    assert created.outcome is WriteOutcome.CREATED

    entity = await target.read(created.ref)

    assert isinstance(entity, DataProduct)
    assert entity.name == "Retail Sales"
    assert entity.description is not None
    assert entity.description.text == "Curated retail data."
    assert entity.identity_for(target.name) == created.ref


async def test_update_is_reflected_on_re_read(target: FakeConnector) -> None:
    created = await target.create(DataProduct(name="Retail Sales"))
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="name", value="Retail Sales v2")],
    )

    result = await target.update(created.ref, diff)

    assert result.outcome is WriteOutcome.UPDATED
    assert result.written_fields == ["name"]
    entity = await target.read(created.ref)
    assert entity.name == "Retail Sales v2"


async def test_reapplying_an_unchanged_diff_is_a_no_op(target: FakeConnector) -> None:
    created = await target.create(DataProduct(name="Retail Sales"))
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="name", value="Retail Sales")],
    )

    result = await target.update(created.ref, diff)

    assert result.outcome is WriteOutcome.NO_OP
    assert result.written_fields == []  # the WriteResult validator forbids a no-op writing fields


async def test_no_op_is_computed_from_checksums_not_string_equality(target: FakeConnector) -> None:
    """A cosmetic difference the checksum rules treat as equal (representational, not a
    real change — trailing/outer whitespace here) still no-ops. If this ever regressed to
    a naive string comparison, this is the test that would catch it.
    """
    created = await target.create(DataProduct(name="Retail Sales"))
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        # Outer whitespace is stripped by canonicalization (envelope.py rule 3).
        changes=[FieldChange(field="name", value="  Retail Sales  ")],
    )

    result = await target.update(created.ref, diff)

    assert result.outcome is WriteOutcome.NO_OP


async def test_a_diff_mixing_changed_and_unchanged_fields_reports_both(
    target: FakeConnector,
) -> None:
    created = await target.create(
        DataProduct(name="Retail Sales", description=TextField.plain("d"))
    )
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[
            FieldChange(field="name", value="Retail Sales"),  # unchanged
            FieldChange(field="description", value={"text": "d2", "format": "plain"}),  # changed
        ],
    )

    result = await target.update(created.ref, diff)

    assert result.outcome is WriteOutcome.UPDATED
    assert result.written_fields == ["description"]
    assert result.skipped_fields == ["name"]


async def test_read_of_an_unknown_ref_raises_not_found(target: FakeConnector) -> None:
    unknown = IdentityRef(
        endpoint=target.name,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="does-not-exist",
        tenant_id=target.tenant_id,
    )

    with pytest.raises(NotFound) as excinfo:
        await target.read(unknown)

    assert excinfo.value.native_key == "does-not-exist"


async def test_update_of_an_unknown_ref_raises_not_found(target: FakeConnector) -> None:
    unknown = IdentityRef(
        endpoint=target.name,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="does-not-exist",
        tenant_id=target.tenant_id,
    )
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT, changes=[FieldChange(field="name", value="x")]
    )

    with pytest.raises(NotFound):
        await target.update(unknown, diff)


async def test_delete_removes_the_object(target: FakeConnector) -> None:
    created = await target.create(DataProduct(name="Retail Sales"))

    await target.delete(created.ref)

    with pytest.raises(NotFound):
        await target.read(created.ref)


async def test_delete_of_an_unknown_ref_raises_not_found(target: FakeConnector) -> None:
    unknown = IdentityRef(
        endpoint=target.name,
        entity_type=EntityType.DATA_PRODUCT,
        native_key="does-not-exist",
        tenant_id=target.tenant_id,
    )

    with pytest.raises(NotFound):
        await target.delete(unknown)


async def test_update_with_a_stale_expected_revision_raises_conflict(
    target: FakeConnector,
) -> None:
    created = await target.create(DataProduct(name="Retail Sales"))
    # Bump the stored revision out from under the caller.
    target.simulate_external_edit(created.ref, {"name": "Edited Elsewhere"})

    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="name", value="My Update")],
        expected_revision="rev-1",
    )

    with pytest.raises(ConflictError) as excinfo:
        await target.update(created.ref, diff)

    assert excinfo.value.expected_revision == "rev-1"
    assert excinfo.value.actual_revision == "rev-2"


async def test_update_with_a_matching_expected_revision_succeeds(target: FakeConnector) -> None:
    created = await target.create(DataProduct(name="Retail Sales"))
    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="name", value="Retail Sales v2")],
        expected_revision="rev-1",
    )

    result = await target.update(created.ref, diff)

    assert result.outcome is WriteOutcome.UPDATED


async def test_simulate_external_edit_is_visible_to_list_changed(target: FakeConnector) -> None:
    created = await target.create(DataProduct(name="Retail Sales"))
    first = await target.list_changed(
        EntityType.DATA_PRODUCT, Watermark.initial(target.name, EntityType.DATA_PRODUCT)
    )

    target.simulate_external_edit(created.ref, {"name": "Edited Elsewhere"})

    second = await target.list_changed(EntityType.DATA_PRODUCT, first.next_watermark)

    assert len(second.changes) == 1
    assert second.changes[0].kind is ChangeKind.UPDATED
    assert second.changes[0].ref == created.ref
