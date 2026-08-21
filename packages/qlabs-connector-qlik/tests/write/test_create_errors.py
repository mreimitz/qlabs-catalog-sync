"""``QlikWriter.create`` — HTTP failure classification and capability honesty.

Covers: a 403 (no create permission in the target space, RS-02 section 5) raises
``AuthError``; a 429 (Tier 2, 100 req/min) is retried and then succeeds; a 5xx that
outlives the retries raises ``TransientError``; a transport failure does too; and — the
capability-honesty rule — an entity type the manifest does not declare creatable is
refused with ``CapabilityError`` **before any request is made**, including a Qlik dataset
(D2) and a glossary term (D5).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError, CapabilityError, TransientError
from qlabs_catalog_sync_sdk.manifest import (
    CapabilityManifest,
    EntityCapability,
    FieldCapability,
)
from qlabs_catalog_sync_sdk.models import (
    Category,
    Dataset,
    EntityType,
    GlossaryTerm,
    TextField,
)
from qlabs_connector_qlik.resolve import QlikReferenceResolver
from qlabs_connector_qlik.write import QlikWriter

from .conftest import (
    DATA_PRODUCTS_URL,
    created_response,
    mock_create,
    refs,
    sales_product,
)


async def test_a_403_raises_auth_error(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    respx_mock.post(DATA_PRODUCTS_URL).mock(
        return_value=httpx.Response(403, json={"errors": [{"code": "FORBIDDEN"}]})
    )
    writer = make_writer()

    with pytest.raises(AuthError) as excinfo:
        await writer.create(sales_product())

    assert excinfo.value.entity_type == EntityType.DATA_PRODUCT.value
    assert excinfo.value.retryable is False
    # A permission problem is not retried by HttpEndpoint — exactly one attempt.
    assert len(respx_mock.calls) == 1


async def test_a_429_is_retried_and_then_succeeds(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    respx_mock.post(DATA_PRODUCTS_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(201, json=created_response()),
        ]
    )
    writer = make_writer()

    result = await writer.create(sales_product())

    assert len(respx_mock.calls) == 3
    assert result.changed is True
    assert result.ref.native_key == created_response()["id"]


async def test_a_5xx_that_outlives_the_retries_raises_transient_error(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    respx_mock.post(DATA_PRODUCTS_URL).mock(return_value=httpx.Response(503))
    writer = make_writer()

    with pytest.raises(TransientError) as excinfo:
        await writer.create(sales_product())

    assert excinfo.value.retryable is True
    # The `make_http` fixture caps attempts at 3.
    assert len(respx_mock.calls) == 3


async def test_a_transport_failure_raises_transient_error(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    respx_mock.post(DATA_PRODUCTS_URL).mock(side_effect=httpx.ConnectError("no route"))
    writer = make_writer()

    with pytest.raises(TransientError):
        await writer.create(sales_product())


async def test_creating_a_qlik_dataset_is_refused_without_any_request(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D2: the manifest declares every dataset field read-only, so create cannot exist."""
    mock_create(respx_mock)
    writer = make_writer()

    with pytest.raises(CapabilityError) as excinfo:
        await writer.create(Dataset(name="orders", description=TextField.plain("...")))

    assert excinfo.value.entity_type == EntityType.DATASET.value
    assert excinfo.value.operation == "create"
    assert excinfo.value.retryable is False
    assert len(respx_mock.calls) == 0


async def test_creating_a_glossary_term_is_refused_without_any_request(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D5: glossary is declared unsupported, not merely unimplemented."""
    mock_create(respx_mock)
    writer = make_writer()

    with pytest.raises(CapabilityError) as excinfo:
        await writer.create(GlossaryTerm(name="EBIT"))

    assert excinfo.value.entity_type == EntityType.GLOSSARY_TERM.value
    assert len(respx_mock.calls) == 0


async def test_creating_a_category_is_refused_without_any_request(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    writer = make_writer()

    with pytest.raises(CapabilityError):
        await writer.create(Category(name="Finance"))

    assert len(respx_mock.calls) == 0


async def test_a_manifest_that_forbids_data_products_blocks_the_write(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Capability honesty is read off the manifest, not hard-coded per entity type."""
    mock_create(respx_mock)
    narrowed = CapabilityManifest(
        entities={EntityType.DATA_PRODUCT: EntityCapability(supported=False)}
    )
    writer = make_writer(manifest=narrowed)

    with pytest.raises(CapabilityError):
        await writer.create(sales_product(dataset_refs=refs(2)))

    assert len(respx_mock.calls) == 0


async def test_a_manifest_with_no_writable_data_product_field_blocks_the_write(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    mock_create(respx_mock)
    read_only = CapabilityManifest(
        entities={
            EntityType.DATA_PRODUCT: EntityCapability(
                identity_keys=["id"], fields={"name": FieldCapability.ro()}
            )
        }
    )
    writer = make_writer(manifest=read_only)

    with pytest.raises(CapabilityError) as excinfo:
        await writer.create(sales_product())

    assert excinfo.value.capability_mode == "ro"
    assert len(respx_mock.calls) == 0


async def test_the_writer_exposes_the_collaborators_t3_5_will_reuse(
    make_writer: Callable[..., QlikWriter],
) -> None:
    """The writer owns the resolver and the manifest, so ``update()`` reuses one cache
    and one declaration rather than building a second of each."""
    writer = make_writer()

    assert isinstance(writer.resolver, QlikReferenceResolver)
    assert writer.manifest.supports(EntityType.DATA_PRODUCT)
    assert writer.manifest.is_writable(EntityType.DATA_PRODUCT, "dataset_refs")
    assert not writer.manifest.is_writable(EntityType.DATASET, "name")
    assert writer.manifest.entity_capability(EntityType.DATA_PRODUCT).max_update_operations == 8
