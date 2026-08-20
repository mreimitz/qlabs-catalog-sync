"""``read.read_dataset`` — GET /api/v1/items/{itemId}.

Covers: secureQri picked out of resourceAttributes and landing as the dataset's
primary native key (with id/resourceId as secondary keys), a legacy-qri fallback when
secureQri is absent, the ETag captured as source_revision, owners/tags mapped from
ownerId/meta.tags, unknown fields surviving in custom_attributes byte-identical, the
fast-fail when a ref carries no item id, and 404/401 classification.
"""

from __future__ import annotations

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, IdentityRef
from qlabs_catalog_sync_sdk.exceptions import AuthError, NotFound
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import AssetType, PartyRole
from qlabs_connector_qlik import read

from .conftest import ENDPOINT, TENANT_BASE_URL, TENANT_ID

ITEM_URL = f"{TENANT_BASE_URL}/api/v1/items/item-1"

FULL_ITEM = {
    "id": "item-1",
    "resourceId": "ds1",
    "resourceType": "dataset",
    "resourceSubType": "qix-df",
    "name": "orders_2026",
    "description": "Orders table",
    "spaceId": "space-123",
    "ownerId": "user-1",
    "meta": {"tags": [{"id": "t1", "name": "finance"}, {"id": "t2", "name": "orders"}]},
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-01-03T00:00:00Z",
    "resourceAttributes": {
        "secureQri": "qdf-secure:tenant-abc:space-123:orders_2026",
        "qri": "qdf:tenant-abc:space-123:orders_2026",
    },
}


def _ref(native_key: str = "item-1", secondary_keys: dict[str, str] | None = None) -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATASET,
        native_key=native_key,
        tenant_id=TENANT_ID,
        secondary_keys={"id": "item-1"} if secondary_keys is None else secondary_keys,
    )


async def test_secure_qri_becomes_the_primary_native_key(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.get(ITEM_URL).mock(return_value=httpx.Response(200, json=FULL_ITEM))

    dataset = await read.read_dataset(http, _ref(), endpoint=ENDPOINT)

    identity = dataset.identity_for(ENDPOINT)
    assert identity is not None
    assert identity.native_key == "qdf-secure:tenant-abc:space-123:orders_2026"
    assert identity.secondary_keys == {"id": "item-1", "resourceId": "ds1"}
    assert identity.tenant_id == TENANT_ID
    assert dataset.physical_ref == "qdf-secure:tenant-abc:space-123:orders_2026"


async def test_legacy_qri_fallback_when_secure_qri_is_absent(
    respx_mock: object, http: HttpEndpoint
) -> None:
    payload = {
        **FULL_ITEM,
        "resourceAttributes": {"qri": "qdf:tenant-abc:space-123:orders_2026"},
    }
    respx_mock.get(ITEM_URL).mock(return_value=httpx.Response(200, json=payload))

    dataset = await read.read_dataset(http, _ref(), endpoint=ENDPOINT)

    identity = dataset.identity_for(ENDPOINT)
    assert identity is not None
    assert identity.native_key == "qdf:tenant-abc:space-123:orders_2026"
    # physical_ref only carries a *secure* qri per RS-03 8.2 — the legacy fallback is
    # good enough for identity, not asserted as the neutral physical_ref value.
    assert dataset.physical_ref is None


async def test_item_id_fallback_when_neither_qri_is_present(
    respx_mock: object, http: HttpEndpoint
) -> None:
    payload = {k: v for k, v in FULL_ITEM.items() if k != "resourceAttributes"}
    respx_mock.get(ITEM_URL).mock(return_value=httpx.Response(200, json=payload))

    dataset = await read.read_dataset(http, _ref(), endpoint=ENDPOINT)

    identity = dataset.identity_for(ENDPOINT)
    assert identity is not None
    assert identity.native_key == "item-1"


async def test_etag_lands_as_source_revision(respx_mock: object, http: HttpEndpoint) -> None:
    respx_mock.get(ITEM_URL).mock(
        return_value=httpx.Response(200, json=FULL_ITEM, headers={"ETag": '"item-etag-456"'})
    )

    dataset = await read.read_dataset(http, _ref(), endpoint=ENDPOINT)

    assert dataset.field_envelopes
    for field_name, envelope in dataset.field_envelopes.items():
        assert envelope.source_revision == '"item-etag-456"', field_name


async def test_owners_and_tags_mapping(respx_mock: object, http: HttpEndpoint) -> None:
    respx_mock.get(ITEM_URL).mock(return_value=httpx.Response(200, json=FULL_ITEM))

    dataset = await read.read_dataset(http, _ref(), endpoint=ENDPOINT)

    assert len(dataset.owners) == 1
    assert dataset.owners[0].party_id == "user-1"
    assert dataset.owners[0].role is PartyRole.OWNER
    assert {tag.key for tag in dataset.tags} == {"finance", "orders"}
    assert dataset.asset_type is AssetType.DATASET


async def test_unknown_fields_survive_byte_identical_in_custom_attributes(
    respx_mock: object, http: HttpEndpoint
) -> None:
    payload = {**FULL_ITEM, "thumbnailId": "thumb-1", "futureField": {"a": [1, 2]}}
    respx_mock.get(ITEM_URL).mock(return_value=httpx.Response(200, json=payload))

    dataset = await read.read_dataset(http, _ref(), endpoint=ENDPOINT)

    assert dataset.custom_attributes["futureField"] == {"a": [1, 2]}
    assert dataset.custom_attributes["thumbnailId"] == "thumb-1"
    assert dataset.custom_attributes["resourceAttributes"] == FULL_ITEM["resourceAttributes"]
    assert dataset.custom_attributes["meta"] == FULL_ITEM["meta"]
    assert dataset.custom_attributes["ownerId"] == "user-1"
    assert dataset.custom_attributes["id"] == "item-1"
    assert dataset.custom_attributes["resourceId"] == "ds1"
    for mapped in ("name", "description"):
        assert mapped not in dataset.custom_attributes


async def test_two_reads_of_identical_data_produce_identical_checksums(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.get(ITEM_URL).mock(return_value=httpx.Response(200, json=FULL_ITEM))

    first = await read.read_dataset(http, _ref(), endpoint=ENDPOINT)
    second = await read.read_dataset(http, _ref(), endpoint=ENDPOINT)

    assert first.field_envelopes.keys() == second.field_envelopes.keys()
    for field_name in first.field_envelopes:
        first_checksum = first.field_envelopes[field_name].checksum
        second_checksum = second.field_envelopes[field_name].checksum
        assert first_checksum == second_checksum


async def test_ref_without_item_id_raises_not_found_without_any_http_call(
    respx_mock: object, http: HttpEndpoint
) -> None:
    ref = _ref(native_key="qdf-secure:tenant-abc:space-123:orders_2026", secondary_keys={})

    with pytest.raises(NotFound):
        await read.read_dataset(http, ref, endpoint=ENDPOINT)

    assert len(respx_mock.calls) == 0


async def test_404_raises_not_found(respx_mock: object, http: HttpEndpoint) -> None:
    respx_mock.get(ITEM_URL).mock(return_value=httpx.Response(404, json={"error": "nope"}))

    with pytest.raises(NotFound):
        await read.read_dataset(http, _ref(), endpoint=ENDPOINT)


async def test_401_raises_auth_error(respx_mock: object, http: HttpEndpoint) -> None:
    respx_mock.get(ITEM_URL).mock(return_value=httpx.Response(401, json={"error": "no"}))

    with pytest.raises(AuthError):
        await read.read_dataset(http, _ref(), endpoint=ENDPOINT)
