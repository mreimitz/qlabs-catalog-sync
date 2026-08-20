"""``concurrency=etag`` (``manifest.py``'s ``qlik_capability_manifest``), exercised
honestly against the real ``Connector`` — the task brief's own words: "the single most
valuable assertion available to you here."

``qlabs_catalog_sync_sdk.conformance.harness.assert_if_match_sent`` exists precisely to
certify a connector that *claims* ETag concurrency actually forwards the revision it read
as ``If-Match`` on write — a manifest/wire mismatch here is exactly what would cause a
real lost update once this connector goes live against a customer's Qlik tenant. Every
test in this module builds its own connector via
:func:`~.conftest.build_connector` (a single respx router, not
:func:`~.conftest.setup_connector`'s ambient one — see that function's docstring) so
``capture_requests``/``assert_if_match_sent`` see the real request objects reliably.

Three things certified here, all read straight off ``write.py``'s own module docstring
(points 12 and 13) rather than assumed:

1. A guarded update (``FieldDiff.expected_revision`` set) sends ``If-Match`` with exactly
   that value.
2. An unguarded update (``expected_revision`` is ``None`` — the diff engine's honest
   answer when the target read carried no ETag) sends **no** ``If-Match`` header at all,
   rather than inventing one — the other half of "does it lie," since a connector that
   always sends *some* ``If-Match`` value regardless of what it actually knows would be
   just as dishonest as one that never sends it.
3. A 412 (the revision no longer matches) triggers exactly one re-read / re-diff /
   re-apply cycle, and the retry PATCH carries the **fresh** ETag from that re-read, not
   the stale one that just failed — proving the recovery path does not simply resend the
   same doomed precondition.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from qlabs_catalog_sync_sdk.conformance.harness import assert_if_match_sent, capture_requests
from qlabs_catalog_sync_sdk.contract import WriteOutcome
from qlabs_catalog_sync_sdk.envelope import to_json_value
from qlabs_catalog_sync_sdk.exceptions import ConflictError
from qlabs_catalog_sync_sdk.models import (
    DataProduct,
    EntityType,
    FieldChange,
    FieldDiff,
    FieldUpdateMode,
)

from .conftest import DATA_PRODUCTS_URL, build_connector, mock_token

CREATED_ID = "conformance-dp-concurrency"
CREATED_QRI = f"qri:data-product://{CREATED_ID}"
PRODUCT_URL = f"{DATA_PRODUCTS_URL}/{CREATED_ID}"
FIRST_ETAG = 'W/"rev-1"'
FRESH_ETAG = 'W/"rev-2"'


def _created_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": CREATED_ID,
        "qri": CREATED_QRI,
        "name": "Sales Analytics Data Product",
        "spaceId": "space-concurrency",
        "ownerId": "owner-1",
        "tenantId": "acme-conformance-tenant",
        "activated": False,
        "activatedOn": [],
        "datasetIds": [],
        "apiConsumableDatasetIds": [],
        "glossaryIds": [],
        "keyContacts": [],
        "pendingChangesCount": 0,
        "createdAt": "2026-08-20T09:00:00Z",
        "createdBy": "owner-1",
        "updatedAt": "2026-08-20T09:00:00Z",
        "updatedBy": "owner-1",
    }
    body.update(overrides)
    return body


def _name_diff(value: str, *, expected_revision: str | None) -> FieldDiff:
    return FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[
            FieldChange(field="name", mode=FieldUpdateMode.PATCH, value=to_json_value(value))
        ],
        expected_revision=expected_revision,
    )


async def _created_ref(respx_mock: respx.MockRouter) -> tuple[object, object]:
    """Create one data product through the real connector and hand back ``(connector,
    ref)`` — every test in this module starts from a genuinely-created product, not a
    hand-built ref, so the ETag it guards against is the one the connector itself would
    have read.

    The ``GET`` route answers ``update()``'s idempotency pre-read (``write.py`` module
    docstring, point 12a). It is registered on *this* router, which respx tries first, so
    the pre-read never falls through to the ``capture_requests`` catch-all a test opens
    around the write itself — leaving that route observing exactly the write, which is
    what these tests are about. Its body still carries the created name, so every diff
    below is a genuine change and no operation is suppressed before it is sent.
    """
    mock_token(respx_mock)
    respx_mock.post(DATA_PRODUCTS_URL).mock(
        return_value=httpx.Response(201, json=_created_body(), headers={"ETag": FIRST_ETAG})
    )
    respx_mock.get(PRODUCT_URL).mock(
        return_value=httpx.Response(200, json=_created_body(), headers={"ETag": FIRST_ETAG})
    )
    connector = await build_connector()
    created = await connector.create(DataProduct(name="Sales Analytics Data Product"))
    assert created.source_revision == FIRST_ETAG
    return connector, created.ref


async def test_a_guarded_update_sends_if_match_with_the_expected_revision(
    respx_mock: respx.MockRouter,
) -> None:
    connector, ref = await _created_ref(respx_mock)
    try:
        diff = _name_diff("New Name", expected_revision=FIRST_ETAG)
        with capture_requests(response=httpx.Response(204, headers={"ETag": FRESH_ETAG})) as route:
            result = await connector.update(ref, diff)

        assert result.outcome is WriteOutcome.UPDATED
        assert route.call_count == 1
        assert_if_match_sent(route.calls, required=True)
        sent = route.calls.last.request
        assert sent.headers["if-match"] == FIRST_ETAG
        assert sent.method == "PATCH"
        body = json.loads(sent.content)
        assert body == [{"op": "replace", "path": "/name", "value": "New Name"}]
    finally:
        await connector.close()


async def test_an_unguarded_update_sends_no_if_match_at_all(
    respx_mock: respx.MockRouter,
) -> None:
    """The other half of concurrency honesty: no revision to guard with (the diff
    engine's honest answer when the target read carried no ETag) must not be papered
    over by inventing one. ``write.py``'s own module docstring (point 13): sent
    unguarded, with a logged warning, rather than refused outright — refusing would make
    ``update()`` unusable on a tenant that returns no ETags at all."""
    connector, ref = await _created_ref(respx_mock)
    try:
        with capture_requests(response=httpx.Response(204, headers={"ETag": FRESH_ETAG})) as route:
            result = await connector.update(ref, _name_diff("New Name", expected_revision=None))

        assert result.outcome is WriteOutcome.UPDATED
        assert route.call_count == 1
        assert_if_match_sent(route.calls, required=False)
        assert "if-match" not in route.calls.last.request.headers
    finally:
        await connector.close()


async def test_a_412_triggers_exactly_one_re_read_re_diff_re_apply_with_the_fresh_etag(
    respx_mock: respx.MockRouter,
) -> None:
    """A concurrent change invalidated ``FIRST_ETAG``: the PATCH guarded with it is
    rejected (412), the connector re-``GET``\\ s, and the retry carries ``FRESH_ETAG`` —
    not the stale value that just failed — proving the recovery path is a genuine re-read,
    not a blind resend of the same doomed precondition (write.py module docstring, point
    12)."""
    connector, ref = await _created_ref(respx_mock)
    try:
        respx_mock.patch(PRODUCT_URL).mock(
            side_effect=[
                httpx.Response(412, headers={"ETag": FRESH_ETAG}),
                httpx.Response(204, headers={"ETag": 'W/"rev-3"'}),
            ]
        )
        # The re-read the conflict-recovery path issues: the product now shows a
        # *different* name than what this stale diff still believes, so nothing here is
        # already-applied and the retry actually needs to send the operation again.
        respx_mock.get(PRODUCT_URL).mock(
            return_value=httpx.Response(
                200,
                json=_created_body(name="Renamed Concurrently"),
                headers={"ETag": FRESH_ETAG},
            )
        )

        result = await connector.update(ref, _name_diff("New Name", expected_revision=FIRST_ETAG))

        assert result.outcome is WriteOutcome.UPDATED
        patch_calls = [call for call in respx_mock.calls if call.request.method == "PATCH"]
        assert len(patch_calls) == 2, "expected exactly one retry, not a retry loop"
        first_if_match = patch_calls[0].request.headers.get("if-match")
        retry_if_match = patch_calls[1].request.headers.get("if-match")
        assert first_if_match == FIRST_ETAG
        assert retry_if_match == FRESH_ETAG, (
            "the retry must guard against the *fresh* revision the re-read returned, "
            "not the stale one that just failed"
        )
    finally:
        await connector.close()


async def test_a_second_412_propagates_rather_than_retrying_again(
    respx_mock: respx.MockRouter,
) -> None:
    """One cycle, never a retry loop (module docstring, point 12): if the retry *also*
    conflicts, that ``ConflictError`` reaches the caller instead of being swallowed."""
    connector, ref = await _created_ref(respx_mock)
    try:
        respx_mock.patch(PRODUCT_URL).mock(
            return_value=httpx.Response(412, headers={"ETag": FRESH_ETAG})
        )
        respx_mock.get(PRODUCT_URL).mock(
            return_value=httpx.Response(
                200, json=_created_body(name="Renamed Concurrently"), headers={"ETag": FRESH_ETAG}
            )
        )

        with pytest.raises(ConflictError):
            await connector.update(ref, _name_diff("New Name", expected_revision=FIRST_ETAG))

        patch_calls = [call for call in respx_mock.calls if call.request.method == "PATCH"]
        assert len(patch_calls) == 2
    finally:
        await connector.close()
