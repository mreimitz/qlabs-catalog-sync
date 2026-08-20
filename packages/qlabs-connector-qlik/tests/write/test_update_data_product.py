"""``QlikWriter.update`` — the JSON Patch body, ``if-match``, and the 204.

Covers the wire contract itself, asserted on the JSON actually sent: one changed field
produces exactly one ``op: "replace"`` at the right path with the right ``value`` shape;
an array change sends the **whole** array; ``op`` is never ``add`` or ``remove``; the
request goes to the ``/v1``-less data-governance path; ``if-match`` carries
``FieldDiff.expected_revision``; ``204 No Content`` is a success; and an empty diff is a
``NO_OP`` that issues no request at all.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from qlabs_catalog_sync_sdk.contract import WriteOutcome
from qlabs_catalog_sync_sdk.models import Party, PartyRole, Tag, TextField
from qlabs_connector_qlik.write import QlikWriter

from .conftest import (
    ETAG,
    SPACE_ID,
    change,
    diff,
    mock_datasets_by_name,
    mock_patch,
    mock_users,
    owner,
    patch_bodies,
    patch_body,
    patch_calls,
    product_ref,
    refs,
)


async def test_one_changed_field_becomes_exactly_one_replace_operation(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    route = mock_patch(respx_mock)
    writer = make_writer()

    result = await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert route.called
    request = patch_calls(respx_mock)[0]
    assert request.method == "PATCH"
    # The data-governance family has no /v1 segment — same as create.
    assert request.url.path == "/api/data-governance/data-products/6672d8b7a182224cbb3f1c26"
    assert patch_body(respx_mock) == [
        {"op": "replace", "path": "/name", "value": "Renamed Product"}
    ]
    assert result.outcome is WriteOutcome.UPDATED
    assert result.written_fields == ["name"]
    assert result.skipped_fields == []
    assert result.detail is None


async def test_the_identity_ref_comes_back_unchanged(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """An update never re-mints identity — the engine's ref is what it gets back."""
    mock_patch(respx_mock)
    writer = make_writer()
    ref = product_ref()

    result = await writer.update(ref, diff(change("name", "Renamed Product")))

    assert result.ref == ref


async def test_a_text_field_change_sends_the_body_not_the_neutral_wrapper(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The diff carries ``{"text": ..., "format": ...}``; Qlik wants a bare string."""
    mock_patch(respx_mock)
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(
            change("description", TextField.plain("Curated sales datasets")),
            change("documentation", TextField.markdown("# Sales\nCurated.")),
        ),
    )

    assert patch_body(respx_mock) == [
        {"op": "replace", "path": "/description", "value": "Curated sales datasets"},
        {"op": "replace", "path": "/readMe", "value": "# Sales\nCurated."},
    ]
    assert result.written_fields == ["description", "documentation"]


async def test_a_null_text_value_clears_the_field_rather_than_sending_an_empty_string(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """RS-02 documents ``null`` for the string paths; the diff's null means "clear it"."""
    mock_patch(respx_mock)
    writer = make_writer()

    await writer.update(product_ref(), diff(change("description", None)))

    assert patch_body(respx_mock) == [
        {"op": "replace", "path": "/description", "value": None}
    ]


async def test_one_changed_tag_sends_the_whole_tags_array(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Array paths are full replace: the complete desired list, never a delta."""
    mock_patch(respx_mock)
    writer = make_writer()
    desired = [Tag(key="sales"), Tag(key="revenue"), Tag(key="tier", value="gold")]

    result = await writer.update(product_ref(), diff(change("tags", desired)))

    assert patch_body(respx_mock) == [
        {"op": "replace", "path": "/tags", "value": ["sales", "revenue", "tier=gold"]}
    ]
    assert result.written_fields == ["tags"]


async def test_an_empty_desired_tag_array_is_sent_as_an_empty_array(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """A source that genuinely reports no tags is asking for the target's to be cleared."""
    mock_patch(respx_mock)
    writer = make_writer()

    await writer.update(product_ref(), diff(change("tags", [])))

    assert patch_body(respx_mock) == [{"op": "replace", "path": "/tags", "value": []}]


async def test_dataset_refs_send_the_whole_resolved_array(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_patch(respx_mock)
    first, second = refs(2)
    writer = make_writer(identity_map={first: "ds-customers", second: "ds-orders"})

    result = await writer.update(
        product_ref(), diff(change("dataset_refs", [first, second]))
    )

    assert patch_body(respx_mock) == [
        {"op": "replace", "path": "/datasetIds", "value": ["ds-customers", "ds-orders"]}
    ]
    assert result.written_fields == ["dataset_refs"]


async def test_owners_send_the_whole_resolved_key_contacts_array(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_patch(respx_mock)
    mock_users(respx_mock, {"ada@acme.example": "user-ada", "bob@acme.example": "user-bob"})
    writer = make_writer()
    owners = [
        owner("ada@acme.example"),
        owner("bob@acme.example", role=PartyRole.STEWARD),
    ]

    result = await writer.update(product_ref(), diff(change("owners", owners)))

    assert patch_body(respx_mock) == [
        {
            "op": "replace",
            "path": "/keyContacts",
            "value": [
                {"userId": "user-ada", "role": "owner"},
                {"userId": "user-bob", "role": "steward"},
            ],
        }
    ]
    assert result.written_fields == ["owners"]


async def test_a_party_with_no_email_still_reaches_the_resolver_as_a_neutral_party(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The diff carries Party JSON; it must be read back as a Party, not a raw dict."""
    mock_patch(respx_mock)
    mock_users(respx_mock, {"ada@acme.example": "user-ada"})
    writer = make_writer()
    owners = [
        Party(display_name="Nameless Team", role=PartyRole.CONTACT),
        owner("ada@acme.example"),
    ]

    result = await writer.update(product_ref(), diff(change("owners", owners)))

    assert patch_body(respx_mock)[0]["value"] == [{"userId": "user-ada", "role": "owner"}]
    assert "owners" in result.written_fields
    assert "owners" in result.skipped_fields
    assert result.detail is not None
    assert "Nameless Team (no_email)" in result.detail


async def test_every_operation_is_a_replace_and_the_order_follows_the_diff(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """``op`` is ``replace`` only — never ``add``, never ``remove`` (RS-02 section 2)."""
    mock_patch(respx_mock)
    mock_users(respx_mock, {"ada@acme.example": "user-ada"})
    member = refs(1)[0]
    writer = make_writer(identity_map={member: "ds-customers"})

    result = await writer.update(
        product_ref(),
        diff(
            change("tags", [Tag(key="sales")]),
            change("name", "Renamed Product"),
            change("owners", [owner("ada@acme.example")]),
            change("dataset_refs", [member]),
            change("documentation", TextField.markdown("# Sales")),
        ),
    )

    body = patch_body(respx_mock)
    assert [operation["op"] for operation in body] == ["replace"] * 5
    assert [operation["path"] for operation in body] == [
        "/tags",
        "/name",
        "/keyContacts",
        "/datasetIds",
        "/readMe",
    ]
    # Reporting order is the module's stable field order, not the diff's arrival order.
    assert result.written_fields == [
        "name",
        "documentation",
        "owners",
        "tags",
        "dataset_refs",
    ]


async def test_if_match_carries_the_diffs_expected_revision(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_patch(respx_mock)
    writer = make_writer()

    await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert patch_calls(respx_mock)[0].headers["if-match"] == ETAG


async def test_no_if_match_header_is_sent_when_the_diff_carries_no_revision(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """An absent precondition is omitted, not sent empty (write.py decision 13)."""
    mock_patch(respx_mock)
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(change("name", "Renamed Product"), expected_revision=None),
    )

    assert "if-match" not in patch_calls(respx_mock)[0].headers
    assert result.outcome is WriteOutcome.UPDATED


async def test_a_204_no_content_is_a_success(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """RS-02: the PATCH returns 204 with no body — nothing may try to parse one."""
    route = mock_patch(respx_mock, status_code=204)
    writer = make_writer()

    result = await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert route.calls[0].response.status_code == 204
    assert result.outcome is WriteOutcome.UPDATED
    assert result.changed is True
    assert result.source_revision is None


async def test_a_204_with_an_etag_reports_it_as_the_new_source_revision(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_patch(respx_mock, headers={"ETag": 'W/"rev-9"'})
    writer = make_writer()

    result = await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert result.source_revision == 'W/"rev-9"'


async def test_an_empty_diff_is_a_no_op_with_zero_requests(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The engine's idempotency claim: replaying a cycle writes nothing at all."""
    mock_patch(respx_mock)
    writer = make_writer()

    result = await writer.update(product_ref(), diff())

    assert result.outcome is WriteOutcome.NO_OP
    assert result.changed is False
    assert result.written_fields == []
    assert result.source_revision == ETAG
    assert len(respx_mock.calls) == 0


async def test_a_no_op_never_sends_an_empty_json_patch_array(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """An empty PATCH body is still a mutating request against a customer's catalog —
    a no-op must issue nothing at all, not a request that happens to change nothing."""
    route = mock_patch(respx_mock)
    mock_users(respx_mock, {})
    writer = make_writer()

    # Both routes to NO_OP: an empty diff, and a diff whose only operation was dropped.
    await writer.update(product_ref(), diff())
    await writer.update(
        product_ref(),
        diff(change("owners", [owner("ghost@acme.example", display_name="Ghost")])),
    )

    assert not route.called
    assert [] not in patch_bodies(respx_mock)


async def test_the_product_id_comes_from_the_secondary_key_when_present(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Same lookup shape as ``read.read_data_product``: ``secondary_keys['id']`` wins."""
    url = "https://acme.eu.qlikcloud.example/api/data-governance/data-products/from-secondary"
    route = respx_mock.patch(url).mock(return_value=httpx.Response(204))
    writer = make_writer()
    ref = product_ref(native_key="from-native", secondary_keys={"id": "from-secondary"})

    await writer.update(ref, diff(change("name", "Renamed Product")))

    assert route.called


async def test_no_code_path_creates_a_dataset_or_a_user_on_the_update_path(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D2/D3 as a whole-module invariant: the only mutating request is the one PATCH."""
    mock_patch(respx_mock)
    mock_datasets_by_name(respx_mock, {"orders": "ds-orders"})
    mock_users(respx_mock, {"ada@acme.example": "user-ada"})
    member = refs(1)[0]
    writer = make_writer(dataset_names={member: "orders"})

    await writer.update(
        product_ref(),
        diff(
            change("dataset_refs", [member]),
            change("owners", [owner("ada@acme.example")]),
        ),
    )

    mutating = [
        (call.request.method, call.request.url.path)
        for call in respx_mock.calls
        if call.request.method != "GET"
    ]
    assert mutating == [
        ("PATCH", "/api/data-governance/data-products/6672d8b7a182224cbb3f1c26")
    ]


async def test_space_id_is_never_touched_by_an_update(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """``/spaceId`` is not in the PATCH enum — the configured space cannot leak into one."""
    mock_patch(respx_mock)
    writer = make_writer()

    await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    body = patch_body(respx_mock)
    assert all(operation["path"] != "/spaceId" for operation in body)
    assert SPACE_ID not in str(body)
