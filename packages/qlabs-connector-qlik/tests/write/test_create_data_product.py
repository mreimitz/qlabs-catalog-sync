"""``QlikWriter.create`` — the POST body, the returned identity, and D5/D7.

Covers: a neutral ``DataProduct`` becomes exactly the documented
``POST /api/data-governance/data-products`` body (asserted field by field on the JSON
actually sent, including the ``/api/v1``-less path); optional fields are omitted rather
than sent empty; ``spaceId`` always comes from config; a missing or blank ``name`` fails
before any HTTP call; the created product's ``id``/``qri`` come back as the
:class:`IdentityRef`; the ETag becomes ``source_revision``; the product is **not**
activated (D7); ``glossaryIds`` is never sent (D5); and key/value tags flatten to Qlik's
``string[]``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, WriteOutcome
from qlabs_catalog_sync_sdk.exceptions import ConnectorError
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    DataProductStatus,
    Tag,
    TextField,
)
from qlabs_connector_qlik.write import QlikWriter

from .conftest import (
    CREATED_ID,
    CREATED_MAIN_ID,
    CREATED_QRI,
    ENDPOINT,
    SPACE_ID,
    TENANT_ID,
    mock_create,
    mock_users,
    owner,
    refs,
    sales_product,
    sent_body,
)


async def test_neutral_data_product_becomes_the_documented_post_body(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    route = mock_create(respx_mock)
    mock_users(respx_mock, {"ada@acme.example": "6909d8524392dbbab822c7f7"})
    member = refs(1)[0]
    writer = make_writer(identity_map={member: "6672d8b7a182224cbb3f1c26"})
    product = sales_product(dataset_refs=[member], owners=[owner("ada@acme.example")])

    result = await writer.create(product)

    assert route.called
    request = respx_mock.calls.last.request
    assert request.method == "POST"
    # The data-governance family has no /v1 segment.
    assert request.url.path == "/api/data-governance/data-products"

    body = sent_body(respx_mock)
    assert body == {
        "name": "Sales Analytics Data Product",
        "spaceId": SPACE_ID,
        "description": "Curated sales datasets",
        "readMe": "# Sales\nCurated sales datasets.",
        "tags": ["sales", "revenue"],
        "datasetIds": ["6672d8b7a182224cbb3f1c26"],
        "keyContacts": [{"userId": "6909d8524392dbbab822c7f7", "role": "owner"}],
    }
    assert result.outcome is WriteOutcome.CREATED
    assert result.written_fields == [
        "name",
        "description",
        "documentation",
        "owners",
        "tags",
        "dataset_refs",
    ]
    assert result.skipped_fields == []
    assert result.detail is None


async def test_optional_fields_are_omitted_rather_than_sent_empty(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    writer = make_writer()

    result = await writer.create(DataProduct(name="Bare Product"))

    assert sent_body(respx_mock) == {"name": "Bare Product", "spaceId": SPACE_ID}
    assert result.written_fields == ["name"]
    assert result.skipped_fields == []


async def test_space_id_always_comes_from_config_and_a_different_placement_is_reported(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    writer = make_writer()

    result = await writer.create(sales_product(placement="main.sales"))

    assert sent_body(respx_mock)["spaceId"] == SPACE_ID
    assert "placement" in result.skipped_fields
    assert "placement" not in result.written_fields
    assert result.detail is not None
    assert "'main.sales'" in result.detail
    assert SPACE_ID in result.detail


async def test_a_placement_that_matches_the_configured_space_counts_as_written(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    writer = make_writer()

    result = await writer.create(sales_product(placement=SPACE_ID))

    assert "placement" in result.written_fields
    assert result.skipped_fields == []


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
async def test_a_blank_name_fails_before_any_http_call(
    respx_mock: object, make_writer: Callable[..., QlikWriter], blank: str
) -> None:
    mock_create(respx_mock)
    writer = make_writer()
    # model_construct bypasses the neutral model's own min_length=1 guard, which is the
    # only way an unnamed product could ever reach the connector.
    product = DataProduct.model_construct(name=blank, dataset_refs=refs(2), owners=[])

    with pytest.raises(ConnectorError) as excinfo:
        await writer.create(product)

    assert "without a name" in str(excinfo.value)
    assert excinfo.value.entity_type == EntityType.DATA_PRODUCT.value
    # Nothing at all was sent — not even the resolver's dataset/user GETs.
    assert len(respx_mock.calls) == 0


async def test_a_missing_name_attribute_fails_before_any_http_call(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    writer = make_writer()
    product = DataProduct.model_construct(dataset_refs=[], owners=[])

    with pytest.raises(ConnectorError):
        await writer.create(product)

    assert len(respx_mock.calls) == 0


async def test_the_created_products_identity_comes_back_from_the_response(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock, headers={"ETag": 'W/"rev-7"'})
    writer = make_writer()

    result = await writer.create(sales_product())

    assert result.ref.endpoint == ENDPOINT
    assert result.ref.entity_type is EntityType.DATA_PRODUCT
    assert result.ref.tenant_id == TENANT_ID
    assert result.ref.native_key == CREATED_ID
    assert result.ref.secondary_keys == {"qri": CREATED_QRI, "mainId": CREATED_MAIN_ID}
    assert result.source_revision == 'W/"rev-7"'


async def test_source_revision_is_none_when_the_response_carries_no_etag(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    writer = make_writer()

    result = await writer.create(sales_product())

    assert result.source_revision is None


async def test_a_response_without_an_id_is_refused_rather_than_guessed(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock, id="")
    writer = make_writer()

    with pytest.raises(ConnectorError, match="no usable 'id'"):
        await writer.create(sales_product())


async def test_the_created_product_is_not_activated(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D7: activation is opt-in and off by default — create() never activates."""
    mock_create(respx_mock)
    activate = respx_mock.post(
        f"https://acme.eu.qlikcloud.example/api/data-governance/data-products/{CREATED_ID}"
        "/actions/activate"
    )
    writer = make_writer()

    result = await writer.create(sales_product(status=DataProductStatus.ACTIVE))

    body = sent_body(respx_mock)
    assert not activate.called
    assert len(respx_mock.calls) == 1
    for activation_key in ("activated", "activatedAt", "activatedOn", "status"):
        assert activation_key not in body
    assert "status" in result.skipped_fields
    assert "status" not in result.written_fields
    assert result.detail is not None
    assert "decision D7" in result.detail


async def test_a_draft_status_is_not_reported_as_skipped(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    writer = make_writer()

    result = await writer.create(sales_product(status=DataProductStatus.DRAFT))

    assert result.skipped_fields == []
    assert result.detail is None


async def test_glossary_ids_are_never_sent_and_neutral_refs_are_reported(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D5: glossary is out of the MVP, so the key is omitted — and said so, not lost."""
    mock_create(respx_mock)
    writer = make_writer()

    result = await writer.create(sales_product(glossary_term_refs=[uuid.uuid4(), uuid.uuid4()]))

    assert "glossaryIds" not in sent_body(respx_mock)
    assert "glossary_term_refs" in result.skipped_fields
    assert result.detail is not None
    assert "decision D5" in result.detail


async def test_key_value_tags_flatten_and_duplicates_collapse(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    writer = make_writer()
    product = sales_product(
        tags=[
            Tag(key="sales"),
            Tag(key="tier", value="gold"),
            Tag(key="tier", value="silver"),
            Tag(key="sales"),
        ]
    )

    await writer.create(product)

    assert sent_body(respx_mock)["tags"] == ["sales", "tier=gold", "tier=silver"]


async def test_a_present_but_empty_description_is_sent_as_an_empty_string(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """An explicitly-empty neutral value is a real value, not an absent one."""
    mock_create(respx_mock)
    writer = make_writer()

    result = await writer.create(DataProduct(name="Bare Product", description=TextField.plain("")))

    body = sent_body(respx_mock)
    assert body["description"] == ""
    assert "readMe" not in body
    assert result.written_fields == ["name", "description"]
