"""Shared fixtures for the neutral-model tests: one fully populated instance per entity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import JsonValue

from qlabs_catalog_sync_sdk.models import (
    AssetLink,
    AssetType,
    Category,
    DataProduct,
    DataProductStatus,
    Dataset,
    EntityType,
    FieldEnvelope,
    GlossaryTerm,
    GlossaryTermStatus,
    IdentityRef,
    Party,
    PartyRole,
    Tag,
    TermRelation,
    TextField,
)

PRODUCT_ID = UUID("11111111-1111-4111-8111-111111111111")
DATASET_ID = UUID("22222222-2222-4222-8222-222222222222")
TERM_ID = UUID("33333333-3333-4333-8333-333333333333")
CATEGORY_ID = UUID("44444444-4444-4444-8444-444444444444")

READ_AT = datetime(2026, 8, 20, 9, 30, 0, tzinfo=UTC)
MODIFIED_AT = datetime(2026, 8, 19, 17, 5, 30, tzinfo=timezone(timedelta(hours=2)))

NESTED_CUSTOM_ATTRIBUTES: dict[str, JsonValue] = {
    "qlik": {
        "spaceId": "s-1",
        "meta": {"tags": ["gold", "finance"], "counts": {"datasets": 3}},
    },
    "databricks": {
        "properties": [
            {"key": "delta.minReaderVersion", "value": 2},
            {"key": "owner_verified", "value": True},
        ],
        "ratio": 0.125,
        "missing": None,
    },
    "emptyList": [],
    "emptyMap": {},
}


def _envelopes() -> dict[str, FieldEnvelope[JsonValue]]:
    return {
        "name": FieldEnvelope(
            value="Retail Sales",
            source_endpoint="databricks",
            source_revision="rev-7",
            last_modified_at=MODIFIED_AT,
            last_synced_at=READ_AT,
            checksum="sha256:deadbeef",
        ),
        "tags": FieldEnvelope(
            value=[{"key": "domain", "value": "sales"}],
            source_endpoint="databricks",
            last_synced_at=READ_AT,
        ),
    }


@pytest.fixture
def nested_custom_attributes() -> dict[str, JsonValue]:
    return NESTED_CUSTOM_ATTRIBUTES


@pytest.fixture
def identity_ref() -> IdentityRef:
    return IdentityRef(
        endpoint="qlik",
        entity_type=EntityType.DATASET,
        native_key="qri:qdf:dataset://abc",
        tenant_id="tenant-a",
        secondary_keys={"id": "ds-1", "resourceId": "res-1"},
    )


@pytest.fixture
def data_product() -> DataProduct:
    return DataProduct(
        neutral_id=PRODUCT_ID,
        identities=[
            IdentityRef(
                endpoint="databricks",
                entity_type=EntityType.DATA_PRODUCT,
                native_key="main.retail",
                tenant_id="acct-123",
                secondary_keys={"metastore_id": "ms-1"},
            ),
            IdentityRef(
                endpoint="qlik",
                entity_type=EntityType.DATA_PRODUCT,
                native_key="dp-9",
                tenant_id="tenant-a",
            ),
        ],
        name="Retail Sales",
        description=TextField.plain("Curated retail sales data."),
        documentation=TextField.markdown("# Retail Sales\n\nSee the **docs**."),
        status=DataProductStatus.ACTIVE,
        owners=[
            Party(
                party_id="u-1",
                display_name="Ada",
                email="ada@example.com",
                role=PartyRole.OWNER,
            ),
            Party(email="ops@example.com", role=PartyRole.CONTACT),
        ],
        tags=[Tag(key="domain", value="sales"), Tag(key="gold")],
        dataset_refs=[DATASET_ID],
        glossary_term_refs=[TERM_ID],
        placement="Shared/Finance",
        custom_attributes=NESTED_CUSTOM_ATTRIBUTES,
        field_envelopes=_envelopes(),
    )


@pytest.fixture
def dataset() -> Dataset:
    return Dataset(
        neutral_id=DATASET_ID,
        identities=[
            IdentityRef(
                endpoint="databricks",
                entity_type=EntityType.DATASET,
                native_key="main.retail.orders",
                tenant_id="acct-123",
                secondary_keys={"object_id": "tbl-42"},
            )
        ],
        name="orders",
        description=TextField.plain("One row per order."),
        owners=[Party(email="ada@example.com", role=PartyRole.OWNER)],
        tags=[Tag(key="pii", value="false")],
        classifications=["internal", "finance"],
        glossary_term_refs=[TERM_ID],
        physical_ref="main.retail.orders",
        asset_type=AssetType.TABLE,
        custom_attributes=NESTED_CUSTOM_ATTRIBUTES,
        field_envelopes=_envelopes(),
    )


@pytest.fixture
def glossary_term() -> GlossaryTerm:
    return GlossaryTerm(
        neutral_id=TERM_ID,
        identities=[
            IdentityRef(
                endpoint="qlik",
                entity_type=EntityType.GLOSSARY_TERM,
                native_key="term-5",
                tenant_id="tenant-a",
            )
        ],
        name="Net Revenue",
        definition=TextField.markdown("Revenue **after** returns."),
        abbreviation="NR",
        category_ref=CATEGORY_ID,
        status=GlossaryTermStatus.VERIFIED,
        tags=[Tag(key="certified")],
        stewards=[Party(display_name="Grace", role=PartyRole.STEWARD)],
        term_relations=[TermRelation(type="relatesTo", target_term_ref=PRODUCT_ID)],
        asset_links=[AssetLink(type="linksTo", target_ref=DATASET_ID)],
        custom_attributes=NESTED_CUSTOM_ATTRIBUTES,
        field_envelopes=_envelopes(),
    )


@pytest.fixture
def category() -> Category:
    return Category(
        neutral_id=CATEGORY_ID,
        identities=[
            IdentityRef(
                endpoint="qlik",
                entity_type=EntityType.CATEGORY,
                native_key="cat-1",
                tenant_id="tenant-a",
            )
        ],
        name="Finance",
        description=TextField.plain("Finance glossary category."),
        parent_category_ref=None,
        custom_attributes=NESTED_CUSTOM_ATTRIBUTES,
        field_envelopes=_envelopes(),
    )
