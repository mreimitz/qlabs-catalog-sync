"""``LifecycleActions.move`` — the documented request, the opt-in gate, error
classification, and the identity-across-a-move assumption.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import WriteOutcome
from qlabs_catalog_sync_sdk.exceptions import AuthError, CapabilityError, TransientError
from qlabs_connector_qlik.lifecycle import DestructiveAction, LifecycleActions

from .conftest import DATA_PRODUCTS_URL, PRODUCT_ID, product_ref, sent_body

MOVE_URL = f"{DATA_PRODUCTS_URL}/{PRODUCT_ID}/actions/move"
TARGET_SPACE_ID = "t4a5r6g7e8t9s0p1a2c3e4id"


async def test_move_issues_the_documented_request(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    # RS-02 does not document a response body for `move`; a bare 204 is the more
    # conservative assumption for what a successful action call returns.
    respx_mock.post(MOVE_URL).mock(return_value=httpx.Response(204))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.MOVE}))
    ref = product_ref()

    result = await lifecycle.move(ref, target_space_id=TARGET_SPACE_ID)

    request = respx_mock.calls.last.request
    assert request.method == "POST"
    assert request.url.path == f"/api/data-governance/data-products/{PRODUCT_ID}/actions/move"
    assert sent_body(respx_mock) == {"spaceId": TARGET_SPACE_ID}
    assert result.outcome is WriteOutcome.UPDATED
    assert result.written_fields == ["placement"]
    # RS-02: a move patches the space, not the identifier — identity is assumed
    # unchanged (TENANT_UNVERIFIED), and there is no response body here to contradict it.
    assert result.ref == ref


async def test_move_is_refused_without_the_opt_in_and_issues_zero_requests(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(MOVE_URL).mock(return_value=httpx.Response(204))
    lifecycle = make_lifecycle()  # default: nothing enabled

    with pytest.raises(CapabilityError) as excinfo:
        await lifecycle.move(product_ref(), target_space_id=TARGET_SPACE_ID)

    assert excinfo.value.operation == "move"
    assert len(respx_mock.calls) == 0


async def test_a_403_raises_auth_error(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(MOVE_URL).mock(
        return_value=httpx.Response(403, json={"errors": [{"code": "FORBIDDEN"}]})
    )
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.MOVE}))

    with pytest.raises(AuthError):
        await lifecycle.move(product_ref(), target_space_id=TARGET_SPACE_ID)

    assert len(respx_mock.calls) == 1


async def test_a_429_is_retried_and_then_succeeds(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(MOVE_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(204),
        ]
    )
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.MOVE}))

    result = await lifecycle.move(product_ref(), target_space_id=TARGET_SPACE_ID)

    assert len(respx_mock.calls) == 2
    assert result.outcome is WriteOutcome.UPDATED


async def test_a_5xx_that_outlives_retries_raises_transient_error(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(MOVE_URL).mock(return_value=httpx.Response(503))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.MOVE}))

    with pytest.raises(TransientError):
        await lifecycle.move(product_ref(), target_space_id=TARGET_SPACE_ID)

    assert len(respx_mock.calls) == 3


async def test_a_response_body_that_disagrees_on_identity_is_honored_not_trusted_blindly(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    """If a move response *does* carry a body and its ``id`` disagrees with the ref that
    was moved, that is the one signal available that the RS-02 assumption did not hold
    this time — the returned ref is rebuilt around the new id rather than silently kept
    stale."""
    new_id = "9999d8b7a182224cbb3f9999"
    respx_mock.post(MOVE_URL).mock(
        return_value=httpx.Response(
            200, json={"id": new_id, "qri": f"qri:data-product://{new_id}"}
        )
    )
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.MOVE}))

    result = await lifecycle.move(product_ref(), target_space_id=TARGET_SPACE_ID)

    assert result.ref.native_key == new_id
    assert result.ref.secondary_keys["id"] == new_id


async def test_a_response_body_that_agrees_on_identity_keeps_the_original_ref(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(MOVE_URL).mock(
        return_value=httpx.Response(200, json={"id": PRODUCT_ID, "spaceId": TARGET_SPACE_ID})
    )
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.MOVE}))
    ref = product_ref()

    result = await lifecycle.move(ref, target_space_id=TARGET_SPACE_ID)

    assert result.ref == ref
