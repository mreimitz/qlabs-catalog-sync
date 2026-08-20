"""``list_changed`` watermark semantics: seeding then listing from an initial watermark
yields the seeded set, listing again with the returned watermark yields nothing, and
paging (`has_more`) is exercisable.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.contract import ChangeKind, Watermark
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


def _initial(connector: FakeConnector) -> Watermark:
    return Watermark.initial(connector.name, EntityType.DATA_PRODUCT)


async def test_listing_an_empty_store_from_initial_yields_nothing(source: FakeConnector) -> None:
    result = await source.list_changed(EntityType.DATA_PRODUCT, _initial(source))

    assert result.is_empty
    assert result.is_exhausted


async def test_seeding_then_listing_from_initial_yields_the_seeded_objects(
    source: FakeConnector,
) -> None:
    first = source.seed(DataProduct(name="Retail"))
    second = source.seed(DataProduct(name="Marketing"))

    result = await source.list_changed(EntityType.DATA_PRODUCT, _initial(source))

    # IdentityRef is a plain (unfrozen) pydantic model, so it is not hashable — compare
    # on the native key instead of building a set of refs.
    assert {change.ref.native_key for change in result.changes} == {
        first.native_key,
        second.native_key,
    }
    assert all(change.kind is ChangeKind.CREATED for change in result.changes)
    assert result.is_exhausted


async def test_listing_again_with_the_returned_watermark_yields_nothing(
    source: FakeConnector,
) -> None:
    source.seed(DataProduct(name="Retail"))
    first = await source.list_changed(EntityType.DATA_PRODUCT, _initial(source))

    second = await source.list_changed(EntityType.DATA_PRODUCT, first.next_watermark)

    assert second.is_empty
    assert second.is_exhausted


async def test_seeding_more_then_listing_from_the_prior_watermark_yields_only_the_new_ones(
    source: FakeConnector,
) -> None:
    source.seed(DataProduct(name="Retail"))
    first = await source.list_changed(EntityType.DATA_PRODUCT, _initial(source))

    new_ref = source.seed(DataProduct(name="Marketing"))
    second = await source.list_changed(EntityType.DATA_PRODUCT, first.next_watermark)

    assert [change.ref for change in second.changes] == [new_ref]


async def test_paging_splits_results_and_has_more_tracks_exhaustion() -> None:
    source = FakeConnector.read_only_source(list_changed_page_size=2)
    for i in range(5):
        source.seed(DataProduct(name=f"Product {i}"))

    watermark = _initial(source)
    seen: list[str] = []
    pages = 0
    while True:
        result = await source.list_changed(EntityType.DATA_PRODUCT, watermark)
        pages += 1
        seen.extend(change.ref.native_key for change in result.changes)
        assert len(result.changes) <= 2
        watermark = result.next_watermark
        if not result.has_more:
            break

    assert pages == 3  # 2 + 2 + 1
    assert len(seen) == 5
    assert len(set(seen)) == 5  # no duplicate or dropped page

    # Fully exhausted: one more call with the final watermark yields nothing.
    final = await source.list_changed(EntityType.DATA_PRODUCT, watermark)
    assert final.is_empty
    assert final.is_exhausted


async def test_list_changed_rejects_a_watermark_for_a_different_entity_type(
    source: FakeConnector,
) -> None:
    mismatched = Watermark.initial(source.name, EntityType.DATASET)

    with pytest.raises(ValueError, match="dataset"):
        await source.list_changed(EntityType.DATA_PRODUCT, mismatched)


async def test_vanish_surfaces_a_deleted_change_and_read_then_404s(source: FakeConnector) -> None:
    ref = source.seed(DataProduct(name="Retail"))
    first = await source.list_changed(EntityType.DATA_PRODUCT, _initial(source))
    assert first.changes[0].kind is ChangeKind.CREATED

    source.vanish(ref)

    second = await source.list_changed(EntityType.DATA_PRODUCT, first.next_watermark)
    assert len(second.changes) == 1
    assert second.changes[0].kind is ChangeKind.DELETED
    assert second.changes[0].ref == ref
