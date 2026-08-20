"""A no-op write is distinguishable from a real one — the engine's idempotency claim."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qlabs_catalog_sync_sdk.contract import (
    EntityType,
    IdentityRef,
    WriteOutcome,
    WriteResult,
)


@pytest.fixture
def ref() -> IdentityRef:
    return IdentityRef(
        endpoint="qlik",
        entity_type=EntityType.DATA_PRODUCT,
        native_key="dp-9",
        tenant_id="tenant-a",
    )


def test_create_reports_the_resulting_identity_and_new_revision(ref: IdentityRef) -> None:
    result = WriteResult.created(ref, source_revision="etag-1", written_fields=["name"])

    assert result.ref == ref
    assert result.outcome is WriteOutcome.CREATED
    assert result.source_revision == "etag-1"
    assert result.written_fields == ["name"]
    assert result.changed


def test_update_reports_what_it_wrote(ref: IdentityRef) -> None:
    result = WriteResult.updated(ref, source_revision="etag-2", written_fields=["description"])

    assert result.outcome is WriteOutcome.UPDATED
    assert result.changed


def test_a_no_op_is_marked_as_such(ref: IdentityRef) -> None:
    result = WriteResult.no_op(ref, source_revision="etag-1", detail="already matched")

    assert result.outcome is WriteOutcome.NO_OP
    assert not result.changed
    assert result.written_fields == []
    assert result.detail == "already matched"


def test_a_no_op_cannot_claim_it_wrote_something(ref: IdentityRef) -> None:
    with pytest.raises(ValidationError, match="no-op write cannot report written fields"):
        WriteResult(ref=ref, outcome=WriteOutcome.NO_OP, written_fields=["description"])


def test_a_partial_write_reports_what_it_omitted(ref: IdentityRef) -> None:
    """Decision D2: unresolved Qlik dataset members are omitted and reported, never invented."""
    result = WriteResult.updated(
        ref,
        written_fields=["description"],
        skipped_fields=["dataset_refs"],
        detail="2 of 5 datasets unresolved in the target space",
    )

    assert result.changed
    assert result.skipped_fields == ["dataset_refs"]
    assert result.detail is not None


def test_a_source_revision_is_optional(ref: IdentityRef) -> None:
    """Not every endpoint hands back an ETag; Databricks is snapshot + checksum only."""
    assert WriteResult.updated(ref).source_revision is None


def test_the_result_round_trips(ref: IdentityRef) -> None:
    result = WriteResult.created(ref, source_revision="etag-1", written_fields=["name"])

    assert WriteResult.model_validate(result.model_dump(mode="json")) == result
    assert WriteResult.model_validate(result.model_dump(mode="json", by_alias=True)) == result
