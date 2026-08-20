"""``QlikWriter.update`` — decisions D2, D3 and D4 on a full-replace array.

Omitting an unresolved member means something different at update than at create: at
create it never appears, at update a full replace would *remove* it from the customer's
product. These tests pin that difference down. A partial resolution is sent and reported;
a resolution that matched nothing at all leaves the array untouched rather than emptying
it; and the 100-item cap and ``apiConsumableDatasetIds`` subset rule still hold.
"""

from __future__ import annotations

from collections.abc import Callable

from qlabs_catalog_sync_sdk.contract import WriteOutcome
from qlabs_catalog_sync_sdk.models import PartyRole, Tag
from qlabs_connector_qlik.write import MAX_DATASET_IDS, QlikWriter

from .conftest import (
    ETAG,
    change,
    diff,
    mock_create,
    mock_datasets_by_name,
    mock_patch,
    mock_users,
    owner,
    patch_body,
    product_ref,
    refs,
    sales_product,
)


async def test_a_partially_resolved_member_list_is_sent_and_reported(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The members that dropped out do not exist in Qlik, so they were never in the
    target's list either — replacing with what did resolve loses nothing."""
    mock_patch(respx_mock)
    mock_datasets_by_name(respx_mock, {"orders": "ds-orders"})
    first, second, third = refs(3)
    writer = make_writer(
        identity_map={first: "ds-customers"},
        dataset_names={second: "orders", third: "returns"},
    )

    result = await writer.update(
        product_ref(), diff(change("dataset_refs", [first, second, third]))
    )

    assert patch_body(respx_mock) == [
        {"op": "replace", "path": "/datasetIds", "value": ["ds-customers", "ds-orders"]}
    ]
    assert "dataset_refs" in result.written_fields
    assert "dataset_refs" in result.skipped_fields
    assert result.detail is not None
    assert "1 of 3 dataset member(s) did not resolve" in result.detail
    assert "'returns' (not_found)" in result.detail


async def test_a_member_list_that_resolves_to_nothing_leaves_dataset_ids_untouched(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Sending ``[]`` here would be a delete in all but name (D2 plus D4)."""
    mock_patch(respx_mock)
    mock_datasets_by_name(respx_mock, {})
    members = refs(2)
    writer = make_writer(dataset_names={members[0]: "orders", members[1]: "returns"})

    result = await writer.update(
        product_ref(), diff(change("dataset_refs", members))
    )

    # No PATCH at all: the only operation the diff asked for could not be built safely.
    assert not any(call.request.method == "PATCH" for call in respx_mock.calls)
    assert result.outcome is WriteOutcome.NO_OP
    assert result.written_fields == []
    assert result.skipped_fields == ["dataset_refs"]
    assert result.source_revision == ETAG
    assert result.detail is not None
    assert "left untouched rather than replaced with an empty list" in result.detail
    assert "decisions D2 and D4" in result.detail


async def test_a_genuinely_empty_desired_member_list_is_sent_as_an_empty_array(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The source saying "no members" is a real value, not a failed lookup."""
    mock_patch(respx_mock)
    writer = make_writer()

    result = await writer.update(product_ref(), diff(change("dataset_refs", [])))

    assert patch_body(respx_mock) == [
        {"op": "replace", "path": "/datasetIds", "value": []}
    ]
    assert result.outcome is WriteOutcome.UPDATED
    assert result.skipped_fields == []


async def test_an_owner_list_that_matches_nobody_leaves_key_contacts_untouched(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Same rule for D3: a users-API miss must not wipe the product's contacts."""
    mock_patch(respx_mock)
    mock_users(respx_mock, {})
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(change("owners", [owner("ghost@acme.example", display_name="Grace Ghost")])),
    )

    assert not any(call.request.method == "PATCH" for call in respx_mock.calls)
    assert result.outcome is WriteOutcome.NO_OP
    assert result.skipped_fields == ["owners"]
    assert result.detail is not None
    assert "decisions D3 and D4" in result.detail
    assert "Grace Ghost (not_found)" in result.detail
    assert "ghost@acme.example" not in result.detail


async def test_a_genuinely_empty_desired_owner_list_is_sent_as_an_empty_array(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_patch(respx_mock)
    writer = make_writer()

    result = await writer.update(product_ref(), diff(change("owners", [])))

    assert patch_body(respx_mock) == [
        {"op": "replace", "path": "/keyContacts", "value": []}
    ]
    assert result.outcome is WriteOutcome.UPDATED


async def test_one_unbuildable_array_does_not_block_the_rest_of_the_diff(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """A dropped array is reported; the fields that *can* be written still are."""
    mock_patch(respx_mock)
    mock_users(respx_mock, {})
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(
            change("name", "Renamed Product"),
            change("owners", [owner("ghost@acme.example", display_name="Grace Ghost")]),
        ),
    )

    assert patch_body(respx_mock) == [
        {"op": "replace", "path": "/name", "value": "Renamed Product"}
    ]
    assert result.outcome is WriteOutcome.UPDATED
    assert result.written_fields == ["name"]
    assert result.skipped_fields == ["owners"]


async def test_a_duplicate_user_id_collapses_to_one_key_contact(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """RS-02: a given userId may appear only once in keyContacts (one role per user)."""
    mock_patch(respx_mock)
    mock_users(
        respx_mock,
        {"ada@acme.example": "user-ada", "ada.lovelace@acme.example": "user-ada"},
    )
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(
            change(
                "owners",
                [
                    owner("ada@acme.example", role=PartyRole.STEWARD),
                    owner("ada.lovelace@acme.example", role=PartyRole.OWNER),
                ],
            )
        ),
    )

    assert patch_body(respx_mock)[0]["value"] == [{"userId": "user-ada", "role": "owner"}]
    assert result.skipped_fields == []


async def test_two_members_resolving_to_the_same_dataset_send_one_unique_id(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """``datasetIds`` is a unique-string array — a collapse is not a silent drop."""
    mock_patch(respx_mock)
    mock_datasets_by_name(respx_mock, {"orders": "ds-orders"})
    first, second = refs(2)
    writer = make_writer(dataset_names={first: "orders", second: "orders"})

    await writer.update(product_ref(), diff(change("dataset_refs", [first, second])))

    assert patch_body(respx_mock)[0]["value"] == ["ds-orders"]


async def test_the_hundred_item_dataset_cap_is_enforced_client_side(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_patch(respx_mock)
    members = refs(MAX_DATASET_IDS + 5)
    identity_map = {member: f"ds-{index:03d}" for index, member in enumerate(members)}
    writer = make_writer(identity_map=identity_map)

    result = await writer.update(product_ref(), diff(change("dataset_refs", members)))

    value = patch_body(respx_mock)[0]["value"]
    assert len(value) == MAX_DATASET_IDS
    assert value == [f"ds-{index:03d}" for index in range(MAX_DATASET_IDS)]
    assert "dataset_refs" in result.skipped_fields
    assert result.detail is not None
    assert f"5 resolved dataset member(s) beyond Qlik's {MAX_DATASET_IDS}-item" in result.detail


async def test_api_consumable_dataset_ids_stay_a_subset_after_drops(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_patch(respx_mock)
    mock_datasets_by_name(respx_mock, {})
    kept, dropped = refs(2)
    writer = make_writer(identity_map={kept: "ds-kept"}, dataset_names={dropped: "gone"})

    result = await writer.update(
        product_ref(),
        diff(change("dataset_refs", [kept, dropped])),
        api_consumable_refs=[kept, dropped],
    )

    body = patch_body(respx_mock)
    assert body[0]["value"] == ["ds-kept"]
    assert body[1]["value"] == ["ds-kept"]
    assert set(body[1]["value"]) <= set(body[0]["value"])
    assert result.detail is not None
    assert "apiConsumableDatasetIds" in result.detail


async def test_api_consumable_dataset_ids_are_filtered_by_the_hundred_item_cap_too(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """A member that resolved but fell off the cap must not survive in the subset."""
    mock_patch(respx_mock)
    members = refs(MAX_DATASET_IDS + 5)
    identity_map = {member: f"ds-{index:03d}" for index, member in enumerate(members)}
    writer = make_writer(identity_map=identity_map)

    await writer.update(
        product_ref(),
        diff(change("dataset_refs", members)),
        api_consumable_refs=members,
    )

    dataset_ids, consumable = (operation["value"] for operation in patch_body(respx_mock))
    assert len(dataset_ids) == MAX_DATASET_IDS
    assert len(consumable) == MAX_DATASET_IDS
    assert set(consumable) <= set(dataset_ids)


async def test_api_consumable_dataset_ids_are_omitted_unless_asked_for(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_patch(respx_mock)
    member = refs(1)[0]
    writer = make_writer(identity_map={member: "ds-kept"})

    await writer.update(product_ref(), diff(change("dataset_refs", [member])))

    paths = [operation["path"] for operation in patch_body(respx_mock)]
    assert "/apiConsumableDatasetIds" not in paths


async def test_the_resolver_cache_is_shared_with_the_create_path(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """One writer, one resolver, one cache — a name resolved during create is not
    looked up again during a later update."""
    mock_patch(respx_mock)
    mock_create(respx_mock)
    items = mock_datasets_by_name(respx_mock, {"orders": "ds-orders"})
    member = refs(1)[0]
    writer = make_writer(dataset_names={member: "orders"})

    await writer.create(sales_product(dataset_refs=[member]))
    await writer.update(product_ref(), diff(change("dataset_refs", [member])))

    assert len(items.calls) == 1


async def test_every_reason_reaches_one_detail_string(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The run report gets datasets and owners together, exactly as at create."""
    mock_patch(respx_mock)
    mock_datasets_by_name(respx_mock, {"orders": "ds-orders"})
    mock_users(respx_mock, {"ada@acme.example": "user-ada"})
    resolved, missing = refs(2)
    writer = make_writer(dataset_names={resolved: "orders", missing: "gone"})

    result = await writer.update(
        product_ref(),
        diff(
            change("tags", [Tag(key="sales")]),
            change("dataset_refs", [resolved, missing]),
            change(
                "owners",
                [
                    owner("ada@acme.example"),
                    owner("ghost@acme.example", display_name="Grace Ghost"),
                ],
            ),
        ),
    )

    assert result.written_fields == ["owners", "tags", "dataset_refs"]
    assert result.skipped_fields == ["owners", "dataset_refs"]
    assert result.detail is not None
    assert "decision D2" in result.detail
    assert "decision D3" in result.detail
