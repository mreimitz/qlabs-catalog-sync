"""``QlikWriter.update`` — the idempotency pre-read (``write.py`` module docstring, 12a).

Connector-level idempotency is an explicit conformance requirement (RS-08 section 9:
"re-applying an unchanged diff is a no-op"), and it is the product's central claim — that
re-running the sync performs no API writes. The engine's checksum diff already guarantees
it on the normal path by never calling ``update()`` at all; a retried, duplicated or
replayed cycle, however, arrives here with a real, fully-populated diff. Without the
pre-read, that becomes a real PATCH into a customer's live tenant.

What this module pins, in order:

1. A diff whose every value the product already carries is a ``NO_OP`` that issues **no
   PATCH at all**, reports no written fields, and says which paths it dropped and why.
2. A diff that is only *partly* current still writes — the remaining operations only.
3. The pre-read is one plain ``GET`` to the product's own URL, before the PATCH, and no
   more than one.
4. A failed pre-read never fails the update: the write proceeds exactly as it did before
   this check existed. It is an optimization, not a precondition.
5. The check compares the same way ``_already_applied`` always has — exact equality,
   biased toward re-sending — so an absent key, a re-ordered array or a decorated object
   is *not* proof and the operation is still sent. Losing a needed write would be far
   worse than sending a redundant one.
6. Nothing about the concurrency story changes: ``if-match`` still carries the diff's own
   revision, not anything the pre-read returned, so the pre-read opens no new lost-update
   window.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import WriteOutcome
from qlabs_catalog_sync_sdk.exceptions import TransientError
from qlabs_catalog_sync_sdk.models import Tag, TextField
from qlabs_connector_qlik.write import QlikWriter

from .conftest import (
    ETAG,
    PRODUCT_URL,
    change,
    diff,
    mock_patch,
    mock_read_product,
    patch_bodies,
    patch_calls,
    product_ref,
    stale_response,
)


async def test_a_replay_of_the_products_current_value_is_a_no_op_with_no_patch(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The defect this module exists for: two identical ``update()`` calls used to send
    two identical PATCHes and report ``UPDATED`` twice."""
    patch_route = mock_patch(respx_mock)
    mock_read_product(respx_mock, name="Renamed Product")
    writer = make_writer()

    result = await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert result.outcome is WriteOutcome.NO_OP
    assert not patch_route.called, "a no-op must issue no write at all, not an inert one"
    assert result.written_fields == []


async def test_the_no_op_reports_which_paths_it_dropped_and_the_products_own_revision(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """``WriteResult`` has to be honest about a no-op: the SDK's own validator refuses a
    ``NO_OP`` that claims written fields, and an operator reading the run report needs to
    know *why* nothing happened. ``source_revision`` is the revision the pre-read actually
    saw, not the possibly-stale one the diff was planned against."""
    mock_patch(respx_mock)
    mock_read_product(respx_mock, name="Renamed Product", tags=["sales"])
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(change("name", "Renamed Product"), change("tags", [Tag(key="sales")])),
    )

    assert result.outcome is WriteOutcome.NO_OP
    assert result.changed is False
    assert result.detail is not None
    assert "/name" in result.detail
    assert "/tags" in result.detail
    assert "already carried" in result.detail
    # FRESH_ETAG is what mock_read_product returns; ETAG is what the diff was built on.
    assert result.source_revision == 'W/"rev-8"'


async def test_only_the_operations_that_are_not_current_are_sent(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """A partly-current diff is not all-or-nothing: what still needs writing is written,
    and the dropped path is reported rather than silently vanishing."""
    mock_patch(respx_mock)
    mock_read_product(respx_mock, name="Renamed Product", tags=["stale"])
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(change("name", "Renamed Product"), change("tags", [Tag(key="sales")])),
    )

    assert result.outcome is WriteOutcome.UPDATED
    assert patch_bodies(respx_mock) == [[{"op": "replace", "path": "/tags", "value": ["sales"]}]]
    # `written_fields` means "fields this call wrote" — the name was already there.
    assert result.written_fields == ["tags"]
    assert result.detail is not None and "/name" in result.detail


async def test_the_pre_read_is_one_plain_get_to_the_products_own_url_before_the_patch(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Reading must never mutate anything, and one write must not become two reads."""
    mock_patch(respx_mock)
    writer = make_writer()

    await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    calls = [(call.request.method, str(call.request.url)) for call in respx_mock.calls]
    assert calls == [("GET", PRODUCT_URL), ("PATCH", PRODUCT_URL)]


async def test_a_failed_pre_read_never_blocks_the_write(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The pre-read exists to *avoid* a write; its failure must not turn a legitimate one
    into an error. Anything really wrong (here: a server that is down) surfaces from the
    PATCH a moment later if it is still wrong then."""
    respx_mock.get(PRODUCT_URL).mock(return_value=httpx.Response(503))
    patch_route = mock_patch(respx_mock)
    writer = make_writer()

    result = await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert result.outcome is WriteOutcome.UPDATED
    assert patch_route.called
    assert patch_bodies(respx_mock) == [
        [{"op": "replace", "path": "/name", "value": "Renamed Product"}]
    ]


async def test_a_pre_read_returning_an_unreadable_body_also_just_proceeds(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """A 200 whose body is not a JSON object is no usable answer either — same rule."""
    respx_mock.get(PRODUCT_URL).mock(return_value=httpx.Response(200, content=b"not json at all"))
    mock_patch(respx_mock)
    writer = make_writer()

    result = await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert result.outcome is WriteOutcome.UPDATED


async def test_a_write_that_really_is_broken_still_raises_from_the_patch(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The other half of "a failed pre-read is swallowed": nothing that matters is
    swallowed *with* it — the tenant being down still reaches the caller."""
    respx_mock.get(PRODUCT_URL).mock(return_value=httpx.Response(503))
    respx_mock.patch(PRODUCT_URL).mock(return_value=httpx.Response(503))
    writer = make_writer()

    with pytest.raises(TransientError):
        await writer.update(product_ref(), diff(change("name", "Renamed Product")))


async def test_an_absent_key_in_the_pre_read_counts_as_not_applied(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """Exactly the rule the 412 re-diff already used: no key, no proof, so it is sent.
    The comparison fails toward re-sending, because a redundant replace is harmless and a
    dropped one is a lost write."""
    respx_mock.get(PRODUCT_URL).mock(
        return_value=httpx.Response(200, json={"id": "6672d8b7a182224cbb3f1c26"})
    )
    mock_patch(respx_mock)
    writer = make_writer()

    await writer.update(product_ref(), diff(change("documentation", TextField.markdown("# Sales"))))

    assert patch_bodies(respx_mock) == [[{"op": "replace", "path": "/readMe", "value": "# Sales"}]]


async def test_a_reordered_array_at_the_target_is_still_sent(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """A set-equal but differently-ordered array is not proof of equality — Qlik's
    ``tags`` is an ordered JSON array and the connector does not decide otherwise."""
    mock_read_product(respx_mock, tags=["revenue", "sales"])
    mock_patch(respx_mock)
    writer = make_writer()

    await writer.update(product_ref(), diff(change("tags", [Tag(key="sales"), Tag(key="revenue")])))

    assert patch_bodies(respx_mock) == [
        [{"op": "replace", "path": "/tags", "value": ["sales", "revenue"]}]
    ]


async def test_the_patch_still_guards_with_the_diffs_revision_not_the_pre_reads(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """The pre-read must open no new lost-update window. It reports a *fresher* ETag than
    the diff was planned against, and the PATCH deliberately ignores it: a change landing
    between the two must still produce a 412, not be silently written over."""
    mock_read_product(respx_mock, name="Someone Else's Name")  # returns FRESH_ETAG
    mock_patch(respx_mock)
    writer = make_writer()

    await writer.update(product_ref(), diff(change("name", "Renamed Product")))

    assert patch_calls(respx_mock)[0].headers["if-match"] == ETAG


async def test_an_empty_diff_still_short_circuits_before_the_pre_read(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """ "No diff, no request" is unchanged and still means *zero* requests — the pre-read
    is only worth making when there is something it could save."""
    mock_patch(respx_mock)
    writer = make_writer()

    result = await writer.update(product_ref(), diff())

    assert result.outcome is WriteOutcome.NO_OP
    assert len(respx_mock.calls) == 0


async def test_the_default_stale_pre_read_suppresses_nothing(
    respx_mock: object, make_writer: Callable[..., QlikWriter]
) -> None:
    """A guard on this module's own fixture: :func:`~.conftest.stale_response` must
    differ from every value the update tests send, or those tests would be passing
    because the pre-read silently dropped the operation they were asserting on."""
    mock_patch(respx_mock)
    writer = make_writer()

    result = await writer.update(
        product_ref(),
        diff(
            change("name", "Renamed Product"),
            change("description", TextField.plain("Curated sales datasets")),
            change("documentation", TextField.markdown("# Sales")),
            change("tags", [Tag(key="sales")]),
        ),
    )

    assert result.outcome is WriteOutcome.UPDATED
    assert [operation["path"] for operation in patch_bodies(respx_mock)[0]] == [
        "/name",
        "/description",
        "/readMe",
        "/tags",
    ]
    assert stale_response()["name"] != "Renamed Product"
