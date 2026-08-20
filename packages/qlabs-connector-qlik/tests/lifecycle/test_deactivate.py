"""``LifecycleActions.deactivate`` — the documented request, the opt-in gate, and error
classification.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import WriteOutcome
from qlabs_catalog_sync_sdk.exceptions import AuthError, CapabilityError, TransientError
from qlabs_connector_qlik.lifecycle import DestructiveAction, LifecycleActions

from .conftest import DATA_PRODUCTS_URL, PRODUCT_ID, product_ref, sent_body

DEACTIVATE_URL = f"{DATA_PRODUCTS_URL}/{PRODUCT_ID}/actions/deactivate"


async def test_deactivate_issues_the_documented_request(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(DEACTIVATE_URL).mock(return_value=httpx.Response(200, json={}))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.DEACTIVATE}))
    ref = product_ref()

    result = await lifecycle.deactivate(ref)

    request = respx_mock.calls.last.request
    assert request.method == "POST"
    assert (
        request.url.path == f"/api/data-governance/data-products/{PRODUCT_ID}/actions/deactivate"
    )
    # No body fields are documented for this endpoint; a well-formed empty object is sent.
    assert sent_body(respx_mock) == {}
    assert result.outcome is WriteOutcome.UPDATED
    assert result.written_fields == ["status"]
    assert result.ref == ref


async def test_deactivate_is_refused_without_the_opt_in_and_issues_zero_requests(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(DEACTIVATE_URL).mock(return_value=httpx.Response(200, json={}))
    lifecycle = make_lifecycle()  # default: nothing enabled

    with pytest.raises(CapabilityError) as excinfo:
        await lifecycle.deactivate(product_ref())

    assert excinfo.value.operation == "deactivate"
    assert len(respx_mock.calls) == 0


async def test_enabling_activate_does_not_also_enable_deactivate(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    """Per-action granularity: opting into one destructive action must not silently
    unlock a sibling one."""
    respx_mock.post(DEACTIVATE_URL).mock(return_value=httpx.Response(200, json={}))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.ACTIVATE}))

    with pytest.raises(CapabilityError):
        await lifecycle.deactivate(product_ref())

    assert len(respx_mock.calls) == 0


async def test_a_403_raises_auth_error(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(DEACTIVATE_URL).mock(
        return_value=httpx.Response(403, json={"errors": [{"code": "FORBIDDEN"}]})
    )
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.DEACTIVATE}))

    with pytest.raises(AuthError):
        await lifecycle.deactivate(product_ref())

    assert len(respx_mock.calls) == 1


async def test_a_429_is_retried_and_then_succeeds(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(DEACTIVATE_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={}),
        ]
    )
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.DEACTIVATE}))

    result = await lifecycle.deactivate(product_ref())

    assert len(respx_mock.calls) == 2
    assert result.outcome is WriteOutcome.UPDATED


async def test_a_5xx_that_outlives_retries_raises_transient_error(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(DEACTIVATE_URL).mock(return_value=httpx.Response(503))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.DEACTIVATE}))

    with pytest.raises(TransientError):
        await lifecycle.deactivate(product_ref())

    assert len(respx_mock.calls) == 3
