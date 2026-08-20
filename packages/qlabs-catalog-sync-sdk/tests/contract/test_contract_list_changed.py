"""`list_changed` returns changes *and* the proposed next watermark (decision D8)."""

from __future__ import annotations

from datetime import UTC, datetime

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

MODIFIED_AT = datetime(2026, 8, 19, 17, 5, 30, tzinfo=UTC)


def _ref(
    entity_type: EntityType = EntityType.DATA_PRODUCT,
    endpoint: str = "databricks",
) -> IdentityRef:
    return IdentityRef(
        endpoint=endpoint,
        entity_type=entity_type,
        native_key="main.retail",
        tenant_id="acct-123",
    )


def _change(**kwargs: object) -> ChangeRef:
    return ChangeRef(ref=_ref(), **kwargs)


def test_a_change_defaults_to_upsert() -> None:
    """A poll-based listing cannot tell created from updated, and says so."""
    change = _change()

    assert change.kind is ChangeKind.UPSERT
    assert not change.is_delete


def test_a_change_carries_enough_to_read_it_plus_what_the_listing_knew() -> None:
    change = _change(
        source_revision="rev-7",
        last_modified_at=MODIFIED_AT,
        display_name="retail",
    )

    assert change.ref.native_key == "main.retail"
    assert change.endpoint == "databricks"
    assert change.entity_type is EntityType.DATA_PRODUCT
    assert change.source_revision == "rev-7"
    assert change.last_modified_at == MODIFIED_AT
    assert change.display_name == "retail"


def test_a_deleted_change_is_flagged() -> None:
    assert _change(kind=ChangeKind.DELETED).is_delete


def test_the_result_pairs_changes_with_the_proposed_next_watermark() -> None:
    mark = Watermark.at("databricks", EntityType.DATA_PRODUCT, MODIFIED_AT)
    result = ListChangedResult(changes=[_change()], next_watermark=mark)

    assert result.next_watermark == mark
    assert len(result.changes) == 1


def test_a_next_watermark_is_mandatory() -> None:
    """There is no "I have nothing to propose" — a connector always names its position."""
    with pytest.raises(ValidationError):
        ListChangedResult(changes=[_change()])


def test_no_more_pages_is_distinguishable_from_no_more_changes() -> None:
    mark = Watermark.at("databricks", EntityType.DATA_PRODUCT, MODIFIED_AT)

    caught_up = ListChangedResult.empty(mark)
    assert caught_up.is_empty
    assert caught_up.is_exhausted
    assert not caught_up.has_more

    more_to_come = ListChangedResult(changes=[_change()], next_watermark=mark, has_more=True)
    assert not more_to_come.is_empty
    assert not more_to_come.is_exhausted

    empty_page_with_more = ListChangedResult(changes=[], next_watermark=mark, has_more=True)
    assert empty_page_with_more.is_empty
    assert not empty_page_with_more.is_exhausted

    last_page = ListChangedResult(changes=[_change()], next_watermark=mark)
    assert not last_page.is_empty
    assert last_page.is_exhausted


def test_changes_must_belong_to_the_watermarks_stream() -> None:
    mark = Watermark.at("databricks", EntityType.DATA_PRODUCT, MODIFIED_AT)

    with pytest.raises(ValidationError, match="not to databricks:data_product"):
        ListChangedResult(
            changes=[ChangeRef(ref=_ref(entity_type=EntityType.DATASET))],
            next_watermark=mark,
        )

    with pytest.raises(ValidationError, match="not to databricks:data_product"):
        ListChangedResult(
            changes=[ChangeRef(ref=_ref(endpoint="qlik"))],
            next_watermark=mark,
        )


def test_the_result_round_trips() -> None:
    result = ListChangedResult(
        changes=[_change(source_revision="rev-7", last_modified_at=MODIFIED_AT)],
        next_watermark=Watermark.from_cursor("databricks", EntityType.DATA_PRODUCT, "tok"),
        has_more=True,
    )

    assert ListChangedResult.model_validate(result.model_dump(mode="json")) == result
    assert ListChangedResult.model_validate(result.model_dump(mode="json", by_alias=True)) == result
