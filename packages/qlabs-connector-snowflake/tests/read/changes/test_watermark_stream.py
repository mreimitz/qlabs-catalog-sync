"""``list_changed`` respects the requested entity type and the watermark's stream.

* A ``since`` watermark for another endpoint or another entity type is rejected outright:
  the engine must never be able to hand this connector a different stream's resume state
  by mistake, and quietly scanning with it would corrupt both streams' censuses.
* The returned ``next_watermark`` is always on exactly the stream that was requested, and
  every ``ChangeRef`` in the result is on that stream too -- which the SDK's own
  ``ListChangedResult`` validator enforces, proven here directly against the real model.
* The encoded high-water mark never moves backwards, even when Snowflake's own clock
  appears to. Two ``CURSOR`` watermarks are deliberately not comparable through
  ``Watermark.is_after`` (a cursor is opaque by definition, so it returns ``False`` for
  the pair), so monotonicity is an invariant this module has to keep itself -- and
  therefore one that has to be tested directly rather than left to the contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qlabs_catalog_sync_sdk.contract import (
    ChangeKind,
    ChangeRef,
    EntityType,
    IdentityRef,
    ListChangedResult,
    Watermark,
)
from qlabs_connector_snowflake.read import StatementClient

from ..conftest import ENDPOINT, TENANT_ID
from .conftest import (
    NOW_1,
    NOW_2,
    StatementRouter,
    high_water,
    instant,
    poll,
    set_now,
    set_tables,
    table_row,
)


async def test_since_for_a_different_endpoint_is_rejected(client: StatementClient) -> None:
    with pytest.raises(ValueError, match="stream"):
        await poll(
            client,
            EntityType.DATASET,
            Watermark.initial("some-other-endpoint", EntityType.DATASET),
        )


async def test_since_for_a_different_entity_type_is_rejected(client: StatementClient) -> None:
    with pytest.raises(ValueError, match="stream"):
        await poll(
            client,
            EntityType.DATASET,
            Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        )


async def test_next_watermark_is_on_the_requested_stream(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1)
    set_tables(router, [table_row("ORDERS")])

    result = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))

    assert result.next_watermark.endpoint == ENDPOINT
    assert result.next_watermark.entity_type is EntityType.DATASET
    assert result.next_watermark.stream_key == f"{ENDPOINT}:dataset"
    assert result.next_watermark.observed_at == instant(NOW_1)


async def test_a_returned_watermark_is_accepted_back_on_the_same_stream(
    client: StatementClient, router: StatementRouter
) -> None:
    """The round trip the engine actually performs: commit the proposed watermark, hand it
    straight back next cycle."""
    set_now(router, NOW_1, NOW_2)
    set_tables(router, [table_row("ORDERS")], [table_row("ORDERS")])

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert second.next_watermark.same_stream_as(first.next_watermark)


async def test_the_high_water_mark_never_moves_backwards(
    client: StatementClient, router: StatementRouter
) -> None:
    """If Snowflake's clock ever reports an earlier instant than the last poll's -- a
    replica behind its peers, a corrected clock -- the watermark holds where it was. Moving
    it backwards would silently widen every later scan without ever being noticed."""
    set_now(router, NOW_2, NOW_1)  # second poll's "now" is three hours *earlier*
    set_tables(router, [table_row("ORDERS")], [table_row("ORDERS")])

    first = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))
    second = await poll(client, EntityType.DATASET, first.next_watermark)

    assert high_water(second) == high_water(first)


def test_a_change_ref_on_the_wrong_stream_is_rejected_by_the_contract() -> None:
    """The SDK's own guard, and why it is safe that this module builds every ``ChangeRef``
    from the same ``endpoint``/``entity_type`` it passes to ``next_watermark``: a result
    that mixed streams would not even construct."""
    watermark = Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    wrong_stream_change = ChangeRef(
        ref=IdentityRef(
            endpoint=ENDPOINT,
            entity_type=EntityType.DATASET,  # disagrees with the watermark above
            native_key="SALES_DB.PUBLIC.ORDERS",
            tenant_id=TENANT_ID,
        ),
        kind=ChangeKind.UPSERT,
    )

    with pytest.raises(ValidationError):
        ListChangedResult(changes=[wrong_stream_change], next_watermark=watermark)
