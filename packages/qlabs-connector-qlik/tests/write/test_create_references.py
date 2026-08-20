"""``QlikWriter.create`` — decisions D2 and D3 on the wire.

Covers: an unresolved dataset member is omitted from ``datasetIds`` **and** reported in
the ``WriteResult``; an unmatched owner likewise never reaches ``keyContacts``;
``apiConsumableDatasetIds`` stays a subset of the ``datasetIds`` actually sent, even after
drops and after the cap; the 100-item cap is enforced client-side; a duplicate ``userId``
collapses to a single ``keyContacts`` entry; and no code path ever creates a Qlik dataset
or a Qlik user.
"""

from __future__ import annotations

from collections.abc import Callable

from qlabs_catalog_sync_sdk.models import PartyRole
from qlabs_connector_qlik.write import MAX_DATASET_IDS, QlikWriter

from .conftest import (
    TENANT_BASE_URL,
    mock_create,
    mock_datasets_by_name,
    mock_users,
    owner,
    refs,
    sales_product,
    sent_body,
)


async def test_unresolved_dataset_members_are_omitted_and_reported(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D2: never invent a Qlik dataset — omit the member and say so."""
    mock_create(respx_mock)
    mock_datasets_by_name(respx_mock, {"orders": "ds-orders"})
    first, second, third = refs(3)
    writer = make_writer(
        identity_map={first: "ds-customers"},
        dataset_names={second: "orders", third: "returns"},
    )

    result = await writer.create(sales_product(dataset_refs=[first, second, third]))

    assert sent_body(respx_mock)["datasetIds"] == ["ds-customers", "ds-orders"]
    # Written (two of three landed) *and* skipped (one did not) — see write.py's
    # reporting convention.
    assert "dataset_refs" in result.written_fields
    assert "dataset_refs" in result.skipped_fields
    assert result.detail is not None
    assert "1 of 3 dataset member(s) did not resolve" in result.detail
    assert "'returns' (not_found)" in result.detail
    # Nothing was created to make the miss go away.
    assert [call.request.method for call in respx_mock.calls].count("POST") == 1


async def test_a_product_whose_members_all_miss_sends_no_dataset_ids_at_all(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    mock_datasets_by_name(respx_mock, {})
    members = refs(2)
    writer = make_writer(dataset_names={members[0]: "orders", members[1]: "returns"})

    result = await writer.create(sales_product(dataset_refs=members))

    assert "datasetIds" not in sent_body(respx_mock)
    assert "dataset_refs" not in result.written_fields
    assert "dataset_refs" in result.skipped_fields


async def test_a_member_with_no_known_name_falls_through_to_tier_1_only(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """No display name means no tier-2 match is possible; tier 1 still resolves."""
    mock_create(respx_mock)
    mock_datasets_by_name(respx_mock, {"orders": "ds-orders"})
    bound, unnamed = refs(2)
    writer = make_writer(identity_map={bound: "ds-customers"})

    result = await writer.create(sales_product(dataset_refs=[bound, unnamed]))

    assert sent_body(respx_mock)["datasetIds"] == ["ds-customers"]
    assert "dataset_refs" in result.skipped_fields


async def test_unmatched_owners_are_dropped_and_reported_without_leaking_the_email(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D3: keyContacts needs a Qlik userId, not an email."""
    mock_create(respx_mock)
    mock_users(respx_mock, {"ada@acme.example": "user-ada"})
    writer = make_writer()
    product = sales_product(
        owners=[
            owner("ada@acme.example", display_name="Ada Lovelace"),
            owner("ghost@acme.example", role=PartyRole.STEWARD, display_name="Grace Ghost"),
        ]
    )

    result = await writer.create(product)

    assert sent_body(respx_mock)["keyContacts"] == [{"userId": "user-ada", "role": "owner"}]
    assert "owners" in result.written_fields
    assert "owners" in result.skipped_fields
    assert result.detail is not None
    assert "1 of 2 owner(s) did not match a Qlik user" in result.detail
    assert "Grace Ghost (not_found)" in result.detail
    assert "ghost@acme.example" not in result.detail


async def test_an_owner_with_no_email_is_reported_as_such(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    writer = make_writer()

    result = await writer.create(
        sales_product(owners=[owner(email=None, display_name="Nameless Team")])
    )

    assert "keyContacts" not in sent_body(respx_mock)
    assert "owners" in result.skipped_fields
    assert result.detail is not None
    assert "Nameless Team (no_email)" in result.detail


async def test_a_duplicate_user_id_collapses_to_one_key_contact(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """RS-02: a given userId may appear only once in keyContacts (one role per user)."""
    mock_create(respx_mock)
    mock_users(
        respx_mock,
        {"ada@acme.example": "user-ada", "ada.lovelace@acme.example": "user-ada"},
    )
    writer = make_writer()
    product = sales_product(
        owners=[
            owner("ada@acme.example", role=PartyRole.STEWARD),
            owner("ada.lovelace@acme.example", role=PartyRole.OWNER),
        ]
    )

    result = await writer.create(product)

    # One entry, and the higher-priority role (owner) wins the collision.
    assert sent_body(respx_mock)["keyContacts"] == [{"userId": "user-ada", "role": "owner"}]
    assert result.skipped_fields == []


async def test_api_consumable_dataset_ids_stay_a_subset_after_drops(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """A member that dropped out of datasetIds must not survive in the consumable subset."""
    mock_create(respx_mock)
    mock_datasets_by_name(respx_mock, {})
    kept, dropped = refs(2)
    writer = make_writer(identity_map={kept: "ds-kept"}, dataset_names={dropped: "gone"})

    result = await writer.create(
        sales_product(dataset_refs=[kept, dropped]),
        api_consumable_refs=[kept, dropped],
    )

    body = sent_body(respx_mock)
    assert body["datasetIds"] == ["ds-kept"]
    assert body["apiConsumableDatasetIds"] == ["ds-kept"]
    assert set(body["apiConsumableDatasetIds"]) <= set(body["datasetIds"])
    assert result.detail is not None
    assert "apiConsumableDatasetIds" in result.detail


async def test_api_consumable_dataset_ids_are_omitted_unless_asked_for(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    member = refs(1)[0]
    writer = make_writer(identity_map={member: "ds-kept"})

    await writer.create(sales_product(dataset_refs=[member]))

    assert "apiConsumableDatasetIds" not in sent_body(respx_mock)


async def test_the_hundred_item_dataset_cap_is_enforced_client_side(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    members = refs(MAX_DATASET_IDS + 5)
    identity_map = {member: f"ds-{index:03d}" for index, member in enumerate(members)}
    writer = make_writer(identity_map=identity_map)

    result = await writer.create(
        sales_product(dataset_refs=members), api_consumable_refs=members
    )

    body = sent_body(respx_mock)
    assert len(body["datasetIds"]) == MAX_DATASET_IDS
    assert body["datasetIds"] == [f"ds-{index:03d}" for index in range(MAX_DATASET_IDS)]
    assert len(body["apiConsumableDatasetIds"]) == MAX_DATASET_IDS
    assert set(body["apiConsumableDatasetIds"]) <= set(body["datasetIds"])
    assert "dataset_refs" in result.skipped_fields
    assert result.detail is not None
    assert f"5 resolved dataset member(s) beyond Qlik's {MAX_DATASET_IDS}-item" in result.detail


async def test_two_members_resolving_to_the_same_dataset_send_one_unique_id(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """RS-02 documents datasetIds as a unique-string array."""
    mock_create(respx_mock)
    mock_datasets_by_name(respx_mock, {"orders": "ds-orders"})
    first, second = refs(2)
    writer = make_writer(dataset_names={first: "orders", second: "orders"})

    await writer.create(sales_product(dataset_refs=[first, second]))

    assert sent_body(respx_mock)["datasetIds"] == ["ds-orders"]


async def test_every_reason_is_enumerated_in_one_detail_string(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The run report gets all of it — datasets, owners, glossary and status together."""
    mock_create(respx_mock)
    mock_datasets_by_name(respx_mock, {})
    mock_users(respx_mock, {})
    member = refs(1)[0]
    writer = make_writer(dataset_names={member: "gone"})

    result = await writer.create(
        sales_product(dataset_refs=[member], owners=[owner("ghost@acme.example")])
    )

    assert result.skipped_fields == ["owners", "dataset_refs"]
    assert result.detail is not None
    assert "decision D2" in result.detail
    assert "decision D3" in result.detail


async def test_no_code_path_creates_a_qlik_dataset_or_user(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D2/D3 as a whole-module invariant: resolution is read-only, so the only mutating
    request the write path ever makes is the single data-product POST."""
    mock_create(respx_mock)
    mock_datasets_by_name(respx_mock, {"orders": "ds-orders"})
    mock_users(respx_mock, {"ada@acme.example": "user-ada"})
    dataset_creates = respx_mock.post(f"{TENANT_BASE_URL}/api/v1/data-sets")
    item_writes = respx_mock.put(f"{TENANT_BASE_URL}/api/v1/items")
    first, second = refs(2)
    writer = make_writer(dataset_names={first: "orders", second: "returns"})

    await writer.create(
        sales_product(dataset_refs=[first, second], owners=[owner("ada@acme.example")])
    )

    assert not dataset_creates.called
    assert not item_writes.called
    mutating = [
        (call.request.method, call.request.url.path)
        for call in respx_mock.calls
        if call.request.method != "GET"
    ]
    assert mutating == [("POST", "/api/data-governance/data-products")]
