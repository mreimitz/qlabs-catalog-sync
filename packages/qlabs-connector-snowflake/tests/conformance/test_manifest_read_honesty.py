"""A read-honesty check the base ``ConnectorConformanceSuite`` has no generic mechanism
for: does ``read()`` actually deliver what the manifest promises for a field it declares
``ro``? The suite's round-trip checks only ever exercise *writable* fields, and skip
entirely for this connector -- every field here is ``ro``/``na`` -- so nothing in the base
kit ever compares a read-only field's promise against what ``read()`` returns.

``FieldCapabilityMode.RO``'s own docstring (SDK ``manifest.py``) is what is being held to:
"the endpoint can express the field but only ever returns it". Each test below wires a
statement router that *would* answer with real rows for the field in question, so a
failure means "the connector never asked", not "there was nothing to find".

**Finding, stated up front: one field is declared ``ro`` and never returned.**
``manifest.py`` declares ``DATA_PRODUCT.dataset_refs`` ``ro`` ("the schema's tables/views
ARE its datasets"), but ``build_schema_data_product`` deliberately leaves it empty, and so
does ``build_listing_data_product``. That is a considered trade-off, not an oversight --
``NeutralEntity`` mints a fresh random ``neutral_id`` per read, so writing member ids into
``dataset_refs`` would change that field's checksum every cycle and destroy the exact
idempotency ``compute_checksum`` exists to provide (the Databricks connector's
``SchemaRead`` documents the identical reasoning). Membership travels instead on
``DataProductRead.datasets``/``.member_object_names``, which ``Connector.read()`` discards
because its return type is one entity. ``test_dataset_refs_is_promised_but_never_returned``
below pins down both halves: the field really is always empty, and the compensating
channel really does carry the membership. Resolving the contradiction means changing the
manifest or the model, neither of which is in T6.6's owned paths -- it is in this task's
report.

**Two native shapes, one manifest entry.** ``DATA_PRODUCT`` covers both a Snowflake schema
and a listing (``manifest.py``'s identity-keys section), and the two carry different
fields: a schema has no ``documentation``, no ``status`` and no lifecycle; a listing has
all three but is never tag-read by ``read_listing``. The ``ro`` declarations are honest
because *some* shape delivers each of them, which is what the tests below assert
shape-by-shape rather than demanding every field from every shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import respx

from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.manifest import FieldCapabilityMode
from qlabs_catalog_sync_sdk.models import (
    AssetType,
    DataProductStatus,
    EntityType,
    TextFormat,
)
from qlabs_connector_snowflake import read

from .conftest import (
    SCHEMA_FQN,
    TABLE,
    TABLE_FQN,
    dataset_ref,
    dataset_statements,
    listing_ref,
    listing_statements,
    mock_statements,
    schema_ref,
    schema_statements,
    setup_connector,
    statement_client,
)


@pytest.fixture
async def connector() -> AsyncIterator[Connector]:
    async with setup_connector() as connector:
        yield connector


# --------------------------------------------------------------------------------------
# The premise: what the manifest actually promises
# --------------------------------------------------------------------------------------


DATASET_RO_FIELDS = (
    "name",
    "description",
    "owners",
    "tags",
    "classifications",
    "physical_ref",
    "asset_type",
)

DATA_PRODUCT_RO_FIELDS = (
    "name",
    "description",
    "documentation",
    "status",
    "owners",
    "tags",
    "dataset_refs",
)


@pytest.mark.parametrize("field_name", DATASET_RO_FIELDS)
async def test_manifest_declares_the_dataset_field_ro(
    connector: Connector, field_name: str
) -> None:
    """Sanity-check the premise before the read tests below rely on it."""
    capability = connector.capabilities().entity_capability(EntityType.DATASET)
    assert capability is not None
    assert capability.fields[field_name].mode is FieldCapabilityMode.RO


@pytest.mark.parametrize("field_name", DATA_PRODUCT_RO_FIELDS)
async def test_manifest_declares_the_data_product_field_ro(
    connector: Connector, field_name: str
) -> None:
    capability = connector.capabilities().entity_capability(EntityType.DATA_PRODUCT)
    assert capability is not None
    assert capability.fields[field_name].mode is FieldCapabilityMode.RO


# --------------------------------------------------------------------------------------
# DATASET: every ro field, delivered
# --------------------------------------------------------------------------------------


async def test_read_dataset_delivers_every_ro_field_the_manifest_promises(
    connector: Connector,
) -> None:
    """All seven ``ro`` fields on ``DATASET``, from one ``Connector.read()`` call.

    The tag statement answers with a curated ``GOVERNANCE.TAGS.COST_CENTER`` tag *and* a
    ``SNOWFLAKE.CORE.PRIVACY_CATEGORY`` system classification, so this also pins down
    ``mapping.py``'s rule that the two never mix: the curated one reaches ``tags``, the
    machine-generated one reaches ``classifications``, and neither leaks into the other.
    """
    statements = dataset_statements()
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        mock_statements(router, statements)
        dataset = await connector.read(dataset_ref())

    assert dataset.name == TABLE
    assert dataset.description is not None
    assert dataset.description.text == "Order header rows, one per checkout."
    assert [party.display_name for party in dataset.owners] == ["SALES_ENGINEER"]
    assert [(tag.key, tag.value) for tag in dataset.tags] == [
        ("GOVERNANCE.TAGS.COST_CENTER", "commerce")
    ]
    assert dataset.classifications == ["PRIVACY_CATEGORY=IDENTIFIER"]
    assert dataset.physical_ref == TABLE_FQN
    assert dataset.asset_type is AssetType.TABLE

    for field_name in DATASET_RO_FIELDS:
        assert field_name in dataset.field_envelopes, (
            f"{field_name} is declared ro but arrived without a field envelope, so the "
            "engine has no provenance or checksum for it"
        )

    assert statements.statements_matching("TAG_REFERENCES"), (
        "read() never issued a TAG_REFERENCES statement, so it cannot be delivering the "
        "tags/classifications its manifest declares ro"
    )


async def test_read_dataset_leaves_na_fields_alone(connector: Connector) -> None:
    """``glossary_term_refs`` is ``na`` -- Snowflake has no native glossary -- and an
    ``na`` field must arrive empty rather than invented."""
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        mock_statements(router, dataset_statements())
        dataset = await connector.read(dataset_ref())

    assert dataset.glossary_term_refs == []
    assert "glossary_term_refs" not in dataset.field_envelopes


# --------------------------------------------------------------------------------------
# DATA_PRODUCT, schema shape
# --------------------------------------------------------------------------------------


async def test_read_schema_delivers_the_ro_fields_a_schema_has(connector: Connector) -> None:
    """A schema-shaped data product: ``name``, ``description``, ``owners`` and ``tags``.

    ``documentation`` and ``status`` are absent by design and that is honest for this
    shape -- a Snowflake schema has no long-form doc surface distinct from its ``COMMENT``
    and no publish lifecycle. The listing shape below is where those two are proven.
    """
    statements = schema_statements()
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        mock_statements(router, statements)
        data_product = await connector.read(schema_ref())

    assert data_product.name == "PUBLIC"
    assert data_product.description is not None
    assert data_product.description.text == "Conformed sales dimensions and facts."
    assert [party.display_name for party in data_product.owners] == ["SYSADMIN"]
    assert [(tag.key, tag.value) for tag in data_product.tags] == [
        ("GOVERNANCE.TAGS.DOMAIN", "sales")
    ]
    for field_name in ("name", "description", "owners", "tags"):
        assert field_name in data_product.field_envelopes

    assert data_product.documentation is None
    assert data_product.status is None
    assert statements.statements_matching("INFORMATION_SCHEMA.TAG_REFERENCES"), (
        "read() never issued the schema's own TAG_REFERENCES statement"
    )


async def test_read_schema_leaves_na_fields_alone(connector: Connector) -> None:
    """``placement`` and ``glossary_term_refs`` are ``na``: Snowflake has no space/domain
    placement analog and no native glossary."""
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        mock_statements(router, schema_statements())
        data_product = await connector.read(schema_ref())

    assert data_product.placement is None
    assert data_product.glossary_term_refs == []
    assert "placement" not in data_product.field_envelopes


# --------------------------------------------------------------------------------------
# DATA_PRODUCT, listing shape
# --------------------------------------------------------------------------------------


async def test_read_listing_delivers_the_ro_fields_only_a_listing_has(
    connector: Connector,
) -> None:
    """``documentation`` and ``status`` -- the two ``DATA_PRODUCT`` ``ro`` fields the
    schema shape cannot supply, which is why the manifest declaring them is honest.

    Also pins the ``subtitle``/``description`` split ``mapping.py`` warns about: the
    listing's short ``subtitle`` becomes the neutral plain-text ``description``, while its
    long-form Markdown ``description`` becomes ``documentation``. Following the spelling
    literally would swap the two.
    """
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        mock_statements(router, listing_statements())
        data_product = await connector.read(listing_ref())

    assert data_product.name == "Daily sales"
    assert data_product.description is not None
    assert data_product.description.text == "Daily sales by region, refreshed nightly"
    assert data_product.description.format is TextFormat.PLAIN
    assert data_product.documentation is not None
    assert data_product.documentation.format is TextFormat.MARKDOWN
    assert data_product.documentation.text.startswith("# Daily sales")
    assert data_product.status is DataProductStatus.ACTIVE
    assert [party.display_name for party in data_product.owners] == ["SALES_PROVIDER"]

    for field_name in ("name", "description", "documentation", "status", "owners"):
        assert field_name in data_product.field_envelopes


async def test_read_listing_does_not_read_tags_and_says_so_by_omission(
    connector: Connector,
) -> None:
    """``read_listing`` issues no tag statement, so a listing's ``tags`` arrive *absent*
    (no envelope) rather than as a falsely-empty list.

    That distinction is the connector's "enrichment degrades" contract (``read.py``): an
    absent field tells the engine to leave the target alone, while an explicit ``[]``
    would assert "this listing has no tags" -- a claim nothing here checked. The manifest's
    ``DATA_PRODUCT.tags`` ``ro`` promise is carried by the schema shape (proven above), not
    by this one.
    """
    statements = listing_statements()
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        mock_statements(router, statements)
        data_product = await connector.read(listing_ref())

    assert data_product.tags == []
    assert "tags" not in data_product.field_envelopes
    assert not statements.statements_matching("TAG_REFERENCES")


# --------------------------------------------------------------------------------------
# The one declared-but-never-returned field
# --------------------------------------------------------------------------------------


async def test_dataset_refs_is_promised_but_never_returned(connector: Connector) -> None:
    """``DATA_PRODUCT.dataset_refs`` is declared ``ro`` and is always empty -- for both
    native shapes -- with membership travelling on a channel ``Connector.read()`` drops.

    Both halves are asserted so this stays a description of a known, reasoned divergence
    rather than a silent gap: the field really is empty on what ``read()`` returns, and
    ``read_schema()``/``read_listing()`` really do carry the member object names. See this
    module's docstring for why the empty field is deliberate (checksum stability) and this
    task's report for what resolving it would take.
    """
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        mock_statements(router, schema_statements())
        from_schema = await connector.read(schema_ref())
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        mock_statements(router, listing_statements())
        from_listing = await connector.read(listing_ref())

    assert from_schema.dataset_refs == []
    assert from_listing.dataset_refs == []
    assert "dataset_refs" not in from_schema.field_envelopes
    assert "dataset_refs" not in from_listing.field_envelopes

    # The compensating channel: the membership is read, it just does not ride on the
    # entity the engine receives from Connector.read().
    async with statement_client() as client:
        with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
            mock_statements(router, schema_statements())
            schema_read = await read.read_schema(client, schema_ref())
        with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
            mock_statements(router, listing_statements())
            listing_read = await read.read_listing(client, listing_ref())

    assert schema_read.member_object_names == [TABLE_FQN]
    assert [dataset.name for dataset in schema_read.datasets] == [TABLE]
    assert listing_read.member_object_names == [TABLE_FQN]
    assert schema_read.data_product.identities[0].native_key == SCHEMA_FQN
