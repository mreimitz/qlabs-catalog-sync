"""``QlikWriter.update`` — HTTP 412, and the single re-read / re-diff / re-apply cycle.

This is the lost-update path, so every test here counts requests as well as asserting
bodies. Covers: a 412 becomes ``ConflictError`` (a status ``auth.py`` does not classify at
all); one 412 triggers exactly one re-read and one retry, guarded by the **fresh** ETag; the
re-diff drops only the operations the product provably already carries and never widens the
field set; a second 412 propagates so the engine decides; and a re-read that hands back no
ETag abandons the retry rather than overwriting a customer's catalog unguarded.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, WriteOutcome
from qlabs_catalog_sync_sdk.exceptions import ConflictError
from qlabs_catalog_sync_sdk.models import Tag, TextField
from qlabs_connector_qlik.write import QlikWriter

from .conftest import (
    DATA_PRODUCTS_URL,
    ETAG,
    FRESH_ETAG,
    PRODUCT_URL,
    change,
    diff,
    mock_datasets_by_name,
    mock_read_product,
    mock_reads,
    patch_bodies,
    patch_calls,
    product_ref,
    read_response,
    refs,
    sales_product,
)


def _conflict_then(*responses: httpx.Response) -> list[httpx.Response]:
    """A 412 followed by whatever the retry should meet."""
    return [httpx.Response(412, headers={"ETag": FRESH_ETAG}), *responses]


async def test_a_412_triggers_exactly_one_reread_and_one_retry(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    patch_route = respx_mock.patch(PRODUCT_URL).mock(
        side_effect=_conflict_then(httpx.Response(204))
    )
    read_route = mock_read_product(respx_mock, name="Someone Else's Name")
    writer = make_writer()

    result = await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert len(patch_route.calls) == 2
    # Two GETs, and only two: the idempotency pre-read before the first attempt
    # (write.py point 12a), then the single re-read the 412 recovery is allowed — never
    # one re-read per attempt.
    assert len(read_route.calls) == 2
    assert result.outcome is WriteOutcome.UPDATED
    assert result.written_fields == ["name"]


async def test_the_retry_is_guarded_by_the_fresh_etag_not_the_dead_one(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """A retry that re-sent the dead ETag would 412 forever; one that sent none would be
    an unguarded overwrite. It sends the revision the re-read just returned."""
    respx_mock.patch(PRODUCT_URL).mock(side_effect=_conflict_then(httpx.Response(204)))
    mock_read_product(respx_mock, name="Someone Else's Name")
    writer = make_writer()

    await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    first, retry = patch_calls(respx_mock)
    assert first.headers["if-match"] == ETAG
    assert retry.headers["if-match"] == FRESH_ETAG


async def test_the_retry_resends_the_same_operations_when_nothing_converged(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    respx_mock.patch(PRODUCT_URL).mock(side_effect=_conflict_then(httpx.Response(204)))
    mock_read_product(respx_mock, name="Someone Else's Name", tags=["stale"])
    writer = make_writer()

    await writer.update(
        product_ref(),
        diff(change("name", "Renamed Product"), change("tags", [Tag(key="sales")])),
    )

    first, retry = patch_bodies(respx_mock)
    assert first == retry
    assert retry == [
        {"op": "replace", "path": "/name", "value": "Renamed Product"},
        {"op": "replace", "path": "/tags", "value": ["sales"]},
    ]


async def test_the_rediff_drops_only_the_operations_the_product_already_carries(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The honest re-diff: an operation that would change nothing is not re-sent."""
    respx_mock.patch(PRODUCT_URL).mock(side_effect=_conflict_then(httpx.Response(204)))
    # The pre-read sees a product that still needs both operations; the concurrent
    # change lands between it and the PATCH, and by the post-412 re-read it has already
    # set the name we wanted, but not the tags. Two different bodies is what a 412
    # *means* — one body for both reads could not produce this situation at all.
    mock_reads(
        respx_mock,
        read_response(name="Someone Else's Name", tags=["stale"]),
        read_response(name="Renamed Product", tags=["stale"]),
    )
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(change("name", "Renamed Product"), change("tags", [Tag(key="sales")])),
    )

    _, retry = patch_bodies(respx_mock)
    assert retry == [{"op": "replace", "path": "/tags", "value": ["sales"]}]
    # `written_fields` means "fields this call wrote" — the name was already there.
    assert result.written_fields == ["tags"]
    assert result.detail is not None
    assert "/name" in result.detail
    assert "already applied" in result.detail


async def test_a_conflict_that_fully_converged_is_a_no_op_and_issues_no_retry(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Someone else applied exactly what we wanted: nothing left to write."""
    patch_route = respx_mock.patch(PRODUCT_URL).mock(
        side_effect=_conflict_then(httpx.Response(204))
    )
    mock_reads(
        respx_mock,
        read_response(name="Someone Else's Name", tags=["stale"]),
        read_response(name="Renamed Product", tags=["sales"]),
    )
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(change("name", "Renamed Product"), change("tags", [Tag(key="sales")])),
    )

    assert len(patch_route.calls) == 1  # the failed first attempt, and nothing after it
    assert result.outcome is WriteOutcome.NO_OP
    assert result.written_fields == []
    assert result.source_revision == FRESH_ETAG
    assert result.detail is not None
    assert "already applied" in result.detail


async def test_the_rediff_never_widens_the_field_set(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """A field that moved at the target but is not in the diff is never touched."""
    respx_mock.patch(PRODUCT_URL).mock(side_effect=_conflict_then(httpx.Response(204)))
    mock_read_product(
        respx_mock,
        name="Someone Else's Name",
        description="a concurrent description nobody asked us to change",
        tags=["someone-elses-tag"],
    )
    writer = make_writer()

    await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    _, retry = patch_bodies(respx_mock)
    assert [operation["path"] for operation in retry] == ["/name"]


async def test_a_second_412_propagates_as_a_conflict_error(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """One cycle, never a loop — the engine holds the state needed to decide what next."""
    patch_route = respx_mock.patch(PRODUCT_URL).mock(
        side_effect=_conflict_then(httpx.Response(412, headers={"ETag": 'W/"rev-9"'}))
    )
    read_route = mock_read_product(respx_mock, name="Someone Else's Name")
    writer = make_writer()

    with pytest.raises(ConflictError) as excinfo:
        await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert len(patch_route.calls) == 2
    # The pre-read plus exactly one re-read — not one re-read per attempt.
    assert len(read_route.calls) == 2
    assert excinfo.value.expected_revision == FRESH_ETAG
    assert excinfo.value.actual_revision == 'W/"rev-9"'
    assert excinfo.value.entity_type == EntityType.DATA_PRODUCT.value
    assert excinfo.value.retryable is True


async def test_a_412_is_a_conflict_error_carrying_both_revisions(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """``auth.classify_response_error`` has no 412 branch; ``write.py`` supplies one."""
    respx_mock.patch(PRODUCT_URL).mock(
        return_value=httpx.Response(412, headers={"ETag": FRESH_ETAG})
    )
    mock_read_product(respx_mock, etag=None)
    writer = make_writer()

    with pytest.raises(ConflictError) as excinfo:
        await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert excinfo.value.expected_revision == ETAG
    assert excinfo.value.actual_revision == FRESH_ETAG
    assert "HTTP 412" in str(excinfo.value)


async def test_a_reread_without_an_etag_abandons_the_retry_rather_than_writing_unguarded(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """An endpoint that just enforced if-match and then returns nothing to match on is a
    contradiction — resolve it by handing the conflict back, not by an unguarded write."""
    patch_route = respx_mock.patch(PRODUCT_URL).mock(
        side_effect=_conflict_then(httpx.Response(204))
    )
    mock_read_product(respx_mock, etag=None, name="Someone Else's Name")
    writer = make_writer()

    with pytest.raises(ConflictError):
        await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert len(patch_route.calls) == 1  # no retry at all


async def test_an_unguarded_update_that_412s_still_reports_the_conflict_honestly(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """No ``expected_revision`` to send means none to report — never a fabricated one."""
    respx_mock.patch(PRODUCT_URL).mock(side_effect=_conflict_then(httpx.Response(204)))
    mock_read_product(respx_mock, name="Someone Else's Name")
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(change("name", "Renamed Product"), expected_revision=None),
    )

    first, retry = patch_calls(respx_mock)
    assert "if-match" not in first.headers
    # The recovery still guards the retry with what the re-read returned.
    assert retry.headers["if-match"] == FRESH_ETAG
    assert result.outcome is WriteOutcome.UPDATED


async def test_an_absent_key_in_the_reread_counts_as_not_applied(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The comparison fails toward re-sending: no key, no proof, so the operation stays."""
    respx_mock.patch(PRODUCT_URL).mock(side_effect=_conflict_then(httpx.Response(204)))
    # A response that simply omits `readMe` — not one that reports it as null.
    respx_mock.get(PRODUCT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"id": "6672d8b7a182224cbb3f1c26", "name": "Someone Else's Name"},
            headers={"ETag": FRESH_ETAG},
        )
    )
    writer = make_writer()

    await writer.update(product_ref(), diff(change("documentation", TextField.markdown("# Sales"))))

    _, retry = patch_bodies(respx_mock)
    assert retry == [{"op": "replace", "path": "/readMe", "value": "# Sales"}]


async def test_a_reordered_array_at_the_target_is_re_sent_rather_than_assumed_equal(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Exact equality only: a set-equal but differently-ordered array is not proof."""
    respx_mock.patch(PRODUCT_URL).mock(side_effect=_conflict_then(httpx.Response(204)))
    mock_read_product(respx_mock, tags=["revenue", "sales"])
    writer = make_writer()

    await writer.update(
        product_ref(),
        diff(change("tags", [Tag(key="sales"), Tag(key="revenue")])),
    )

    _, retry = patch_bodies(respx_mock)
    assert retry == [{"op": "replace", "path": "/tags", "value": ["sales", "revenue"]}]


async def test_the_reread_uses_the_same_product_url_and_is_a_plain_get(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Re-reading must never mutate anything — D2/D4 hold on the recovery path too."""
    respx_mock.patch(PRODUCT_URL).mock(side_effect=_conflict_then(httpx.Response(204)))
    mock_read_product(respx_mock, name="Someone Else's Name")
    writer = make_writer()

    await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    methods = [call.request.method for call in respx_mock.calls]
    # GET (idempotency pre-read), PATCH (412), GET (recovery re-read), PATCH (retry).
    assert methods == ["GET", "PATCH", "GET", "PATCH"]
    get_request = next(call.request for call in respx_mock.calls if call.request.method == "GET")
    assert get_request.url.path == "/api/data-governance/data-products/6672d8b7a182224cbb3f1c26"


async def test_a_412_on_the_create_path_also_becomes_a_conflict_error(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The interception lives on the shared ``_send`` seam, so create inherits it too —
    with ``expected_revision`` ``None``, because create never sends a precondition."""
    respx_mock.post(DATA_PRODUCTS_URL).mock(return_value=httpx.Response(412))
    writer = make_writer()

    with pytest.raises(ConflictError) as excinfo:
        await writer.create(sales_product())

    assert excinfo.value.expected_revision is None
    assert excinfo.value.actual_revision is None


async def test_a_conflict_still_reports_the_references_that_were_dropped(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """D2's reporting survives the recovery path — the run report loses nothing."""
    respx_mock.patch(PRODUCT_URL).mock(side_effect=_conflict_then(httpx.Response(204)))
    mock_read_product(respx_mock, datasetIds=["ds-stale"])
    mock_datasets_by_name(respx_mock, {})
    kept, missing = refs(2)
    writer = make_writer(identity_map={kept: "ds-kept"}, dataset_names={missing: "gone"})

    result = await writer.update(product_ref(), diff(change("dataset_refs", [kept, missing])))

    assert result.outcome is WriteOutcome.UPDATED
    assert result.detail is not None
    assert "decision D2" in result.detail
    assert "'gone' (not_found)" in result.detail
