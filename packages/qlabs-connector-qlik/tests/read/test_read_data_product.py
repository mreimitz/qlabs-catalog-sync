"""``read.read_data_product`` — GET /api/data-governance/data-products/{id}.

Covers: full field mapping (name/description/readMe/tags/placement), keyContacts +
ownerId becoming deduped Party owners with roles, activated -> status, ``datasetIds``
mapping back onto ``dataset_refs`` through the injected reverse identity seam (module
docstring, point 4) and declining to report the field when it cannot, the ETag landing
as source_revision on every field envelope, unknown/extra fields surviving in
custom_attributes byte-identical, checksum idempotency across two identical reads, and
404/401 classification.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, IdentityRef
from qlabs_catalog_sync_sdk.exceptions import AuthError, NotFound
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import DataProductStatus, PartyRole, TextFormat
from qlabs_connector_qlik import read

from .conftest import ENDPOINT, TENANT_BASE_URL, TENANT_ID

DATA_PRODUCT_URL = f"{TENANT_BASE_URL}/api/data-governance/data-products/dp1"

FULL_PAYLOAD = {
    "id": "dp1",
    "qri": "qri:data-product://dp1",
    "mainId": "dp1-main",
    "tenantId": "tenant-abc",
    "name": "Customer 360",
    "description": "Governed customer master data product",
    "readMe": "# Customer 360\nDaily-refreshed customer master...",
    "spaceId": "space-123",
    "tags": ["customer", "gold"],
    "datasetIds": ["ds1", "ds2"],
    "apiConsumableDatasetIds": ["ds1"],
    "glossaryIds": ["123e4567-e89b-12d3-a456-426614174000"],
    "keyContacts": [
        {"userId": "user-1", "role": "owner"},
        {"userId": "user-2", "role": "steward"},
    ],
    "ownerId": "user-1",
    "activated": True,
    "activatedAt": "2026-01-05T00:00:00Z",
    "activatedOn": ["space-123"],
    "createdAt": "2026-01-01T00:00:00Z",
    "createdBy": "user-1",
    "updatedAt": "2026-01-06T00:00:00Z",
    "updatedBy": "user-1",
    "pendingChangesCount": 0,
    # An undocumented / future field (also plausibly real per the changelog operator
    # path list in the two-way-sync-readiness notes) — proves round-trip fidelity for
    # anything this module does not explicitly understand.
    "semanticModel": {"foo": "bar", "nested": [1, 2, 3]},
}


def _ref_lookup(bindings: Mapping[str, uuid.UUID]) -> read.DatasetRefLookup:
    """A :data:`~qlabs_connector_qlik.read.DatasetRefLookup` over an in-memory map.

    Stands in for the engine's IdentityMap exactly as ``tests/write/conftest.py``'s
    ``identity_lookup`` does for the forward direction: a bare native id in, a bare
    neutral ``UUID`` (or ``None``) out, and no HTTP anywhere in it.
    """

    async def _lookup(native_id: str) -> uuid.UUID | None:
        return bindings.get(native_id)

    return _lookup


def _ref(native_key: str = "dp1") -> IdentityRef:
    return IdentityRef(
        endpoint=ENDPOINT,
        entity_type=EntityType.DATA_PRODUCT,
        native_key=native_key,
        tenant_id=TENANT_ID,
    )


async def test_full_field_mapping(respx_mock: object, http: HttpEndpoint) -> None:
    respx_mock.get(DATA_PRODUCT_URL).mock(
        return_value=httpx.Response(200, json=FULL_PAYLOAD, headers={"ETag": '"etag-abc-123"'})
    )

    product = await read.read_data_product(http, _ref(), endpoint=ENDPOINT)

    assert product.name == "Customer 360"
    assert product.description is not None
    assert product.description.text == "Governed customer master data product"
    assert product.description.format == TextFormat.PLAIN
    assert product.documentation is not None
    assert product.documentation.text.startswith("# Customer 360")
    assert product.documentation.format == TextFormat.MARKDOWN
    assert product.placement == "space-123"
    assert {tag.key for tag in product.tags} == {"customer", "gold"}
    assert product.status is DataProductStatus.ACTIVE


async def test_key_contacts_and_owner_id_become_deduped_parties(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=FULL_PAYLOAD))

    product = await read.read_data_product(http, _ref(), endpoint=ENDPOINT)

    by_id = {party.party_id: party.role for party in product.owners}
    assert by_id == {"user-1": PartyRole.OWNER, "user-2": PartyRole.STEWARD}
    # ownerId ("user-1") also appears in keyContacts with role "owner" — deduped, not
    # doubled.
    assert len(product.owners) == 2


async def test_identity_carries_qri_and_main_id_as_secondary_keys(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=FULL_PAYLOAD))

    product = await read.read_data_product(http, _ref(), endpoint=ENDPOINT)

    identity = product.identity_for(ENDPOINT)
    assert identity is not None
    assert identity.native_key == "dp1"
    assert identity.tenant_id == TENANT_ID
    assert identity.secondary_keys == {"qri": "qri:data-product://dp1", "mainId": "dp1-main"}


async def test_etag_lands_as_source_revision_on_every_field_envelope(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.get(DATA_PRODUCT_URL).mock(
        return_value=httpx.Response(200, json=FULL_PAYLOAD, headers={"ETag": '"etag-xyz"'})
    )

    product = await read.read_data_product(http, _ref(), endpoint=ENDPOINT)

    assert product.field_envelopes  # sanity: something was built
    for field_name, envelope in product.field_envelopes.items():
        assert envelope.source_revision == '"etag-xyz"', field_name


async def test_missing_etag_leaves_source_revision_none(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=FULL_PAYLOAD))

    product = await read.read_data_product(http, _ref(), endpoint=ENDPOINT)

    for envelope in product.field_envelopes.values():
        assert envelope.source_revision is None


async def test_unknown_and_unmapped_fields_survive_byte_identical_in_custom_attributes(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=FULL_PAYLOAD))

    product = await read.read_data_product(http, _ref(), endpoint=ENDPOINT)

    assert product.custom_attributes["semanticModel"] == {"foo": "bar", "nested": [1, 2, 3]}
    assert product.custom_attributes["datasetIds"] == ["ds1", "ds2"]
    assert product.custom_attributes["apiConsumableDatasetIds"] == ["ds1"]
    assert product.custom_attributes["glossaryIds"] == ["123e4567-e89b-12d3-a456-426614174000"]
    assert product.custom_attributes["keyContacts"] == FULL_PAYLOAD["keyContacts"]
    assert product.custom_attributes["ownerId"] == "user-1"
    assert product.custom_attributes["activated"] is True
    assert product.custom_attributes["pendingChangesCount"] == 0
    assert product.custom_attributes["id"] == "dp1"
    assert product.custom_attributes["qri"] == "qri:data-product://dp1"
    # Cleanly, losslessly mapped fields are NOT duplicated into custom_attributes.
    for mapped in ("name", "description", "readMe", "tags", "spaceId"):
        assert mapped not in product.custom_attributes


async def test_dataset_refs_and_glossary_term_refs_are_left_unresolved(
    respx_mock: object, http: HttpEndpoint
) -> None:
    """With no reverse identity seam wired — the connector's own honest default — neither
    ref field is reported. Not reported is not the same as reported empty: ``[]`` would
    assert "this product has no members", which this read cannot know (module docstring,
    point 4). ``glossary_term_refs`` is never reported at all (decision D5)."""
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=FULL_PAYLOAD))

    product = await read.read_data_product(http, _ref(), endpoint=ENDPOINT)

    assert product.dataset_refs == []
    assert product.glossary_term_refs == []
    assert "dataset_refs" not in product.field_envelopes
    assert "glossary_term_refs" not in product.field_envelopes


async def test_dataset_refs_map_back_through_the_reverse_identity_seam(
    respx_mock: object, http: HttpEndpoint
) -> None:
    """The manifest declares ``dataset_refs`` ``rw``, so a read after a write has to be
    able to show the membership the write placed. Order follows ``datasetIds``."""
    first, second = uuid.uuid4(), uuid.uuid4()
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=FULL_PAYLOAD))

    product = await read.read_data_product(
        http,
        _ref(),
        endpoint=ENDPOINT,
        dataset_ref_lookup=_ref_lookup({"ds1": first, "ds2": second}),
    )

    assert product.dataset_refs == [first, second]
    assert "dataset_refs" in product.field_envelopes
    # The mapping is lossy by construction, so the raw ids stay recoverable.
    assert product.custom_attributes["datasetIds"] == ["ds1", "ds2"]


async def test_one_unbound_member_leaves_the_whole_field_unreported(
    respx_mock: object, http: HttpEndpoint
) -> None:
    """All-or-nothing. Reporting just the member that mapped would assert the product's
    membership *is* that one member — a different and false claim, and exactly the kind
    of invention decision D2 forbids. So the field is not reported at all."""
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=FULL_PAYLOAD))

    product = await read.read_data_product(
        http, _ref(), endpoint=ENDPOINT, dataset_ref_lookup=_ref_lookup({"ds1": uuid.uuid4()})
    )

    assert product.dataset_refs == []
    assert "dataset_refs" not in product.field_envelopes


async def test_an_empty_dataset_ids_array_is_reported_as_an_empty_dataset_refs(
    respx_mock: object, http: HttpEndpoint
) -> None:
    """A product Qlik says has no datasets maps completely, to nothing — so unlike the
    cases above this *is* a value the connector can state, and it is reported with an
    envelope (``envelope.py``'s rule 10: present-but-empty is a real value)."""
    payload = {**FULL_PAYLOAD, "datasetIds": []}
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=payload))

    product = await read.read_data_product(
        http, _ref(), endpoint=ENDPOINT, dataset_ref_lookup=_ref_lookup({})
    )

    assert product.dataset_refs == []
    assert "dataset_refs" in product.field_envelopes


async def test_an_absent_dataset_ids_key_is_not_reported_at_all(
    respx_mock: object, http: HttpEndpoint
) -> None:
    """Nothing to map is not the same as mapping to nothing."""
    payload = {key: value for key, value in FULL_PAYLOAD.items() if key != "datasetIds"}
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=payload))

    product = await read.read_data_product(
        http, _ref(), endpoint=ENDPOINT, dataset_ref_lookup=_ref_lookup({})
    )

    assert "dataset_refs" not in product.field_envelopes


async def test_two_native_ids_bound_to_one_neutral_id_collapse_to_one_ref(
    respx_mock: object, http: HttpEndpoint
) -> None:
    """The mirror of ``write.py``'s ``_dedupe`` on the way out: two members can
    legitimately resolve to one Qlik dataset, so the way back must not double-count."""
    member = uuid.uuid4()
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=FULL_PAYLOAD))

    product = await read.read_data_product(
        http,
        _ref(),
        endpoint=ENDPOINT,
        dataset_ref_lookup=_ref_lookup({"ds1": member, "ds2": member}),
    )

    assert product.dataset_refs == [member]


async def test_the_reverse_seam_never_adds_an_http_call(
    respx_mock: object, http: HttpEndpoint
) -> None:
    """The seam is a pure IdentityMap question the engine answers in memory, so mapping
    the members back must not turn one documented ``GET`` into several."""
    route = respx_mock.get(DATA_PRODUCT_URL).mock(
        return_value=httpx.Response(200, json=FULL_PAYLOAD)
    )

    await read.read_data_product(
        http,
        _ref(),
        endpoint=ENDPOINT,
        dataset_ref_lookup=_ref_lookup({"ds1": uuid.uuid4(), "ds2": uuid.uuid4()}),
    )

    assert len(route.calls) == 1
    assert len(respx_mock.calls) == 1


async def test_two_reads_of_identical_data_produce_identical_checksums(
    respx_mock: object, http: HttpEndpoint
) -> None:
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=FULL_PAYLOAD))

    first = await read.read_data_product(http, _ref(), endpoint=ENDPOINT)
    second = await read.read_data_product(http, _ref(), endpoint=ENDPOINT)

    assert first.neutral_id != second.neutral_id  # each read gets a fresh engine-side id
    assert first.field_envelopes.keys() == second.field_envelopes.keys()
    for field_name in first.field_envelopes:
        first_checksum = first.field_envelopes[field_name].checksum
        second_checksum = second.field_envelopes[field_name].checksum
        assert first_checksum == second_checksum


async def test_inactive_product_maps_to_draft_status(
    respx_mock: object, http: HttpEndpoint
) -> None:
    payload = {**FULL_PAYLOAD, "activated": False}
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(200, json=payload))

    product = await read.read_data_product(http, _ref(), endpoint=ENDPOINT)

    assert product.status is DataProductStatus.DRAFT


async def test_404_raises_not_found(respx_mock: object, http: HttpEndpoint) -> None:
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(404, json={"error": "nope"}))

    with pytest.raises(NotFound):
        await read.read_data_product(http, _ref(), endpoint=ENDPOINT)


async def test_401_raises_auth_error(respx_mock: object, http: HttpEndpoint) -> None:
    respx_mock.get(DATA_PRODUCT_URL).mock(return_value=httpx.Response(401, json={"error": "no"}))

    with pytest.raises(AuthError):
        await read.read_data_product(http, _ref(), endpoint=ENDPOINT)
