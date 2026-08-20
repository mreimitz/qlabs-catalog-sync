"""``LifecycleActions.delete`` — the documented request, the opt-in gate, error
classification, and the ABC's ``-> None`` return.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError, CapabilityError, NotFound, TransientError
from qlabs_connector_qlik.lifecycle import DestructiveAction, LifecycleActions

from .conftest import DATA_PRODUCTS_URL, PRODUCT_ID, product_ref

DELETE_URL = f"{DATA_PRODUCTS_URL}/{PRODUCT_ID}"


async def test_delete_issues_the_documented_request_and_returns_none(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.delete(DELETE_URL).mock(return_value=httpx.Response(204))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.DELETE}))

    result = await lifecycle.delete(product_ref())

    request = respx_mock.calls.last.request
    assert request.method == "DELETE"
    assert request.url.path == f"/api/data-governance/data-products/{PRODUCT_ID}"
    assert result is None
    assert len(respx_mock.calls) == 1


async def test_delete_is_refused_without_the_opt_in_and_issues_zero_requests(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.delete(DELETE_URL).mock(return_value=httpx.Response(204))
    lifecycle = make_lifecycle()  # default: nothing enabled

    with pytest.raises(CapabilityError) as excinfo:
        await lifecycle.delete(product_ref())

    assert excinfo.value.operation == "delete"
    assert excinfo.value.retryable is False
    assert len(respx_mock.calls) == 0


async def test_enabling_activate_does_not_also_enable_delete(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    """The exact failure mode the task exists to prevent: a signature-compatible
    ``delete(ref)`` call must stay refused even once *some* destructive action has been
    opted into for this connector instance."""
    respx_mock.delete(DELETE_URL).mock(return_value=httpx.Response(204))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.ACTIVATE}))

    with pytest.raises(CapabilityError):
        await lifecycle.delete(product_ref())

    assert len(respx_mock.calls) == 0


async def test_a_403_raises_auth_error(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.delete(DELETE_URL).mock(
        return_value=httpx.Response(403, json={"errors": [{"code": "FORBIDDEN"}]})
    )
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.DELETE}))

    with pytest.raises(AuthError) as excinfo:
        await lifecycle.delete(product_ref())

    assert excinfo.value.retryable is False
    assert len(respx_mock.calls) == 1


async def test_a_404_raises_not_found(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.delete(DELETE_URL).mock(return_value=httpx.Response(404))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.DELETE}))

    with pytest.raises(NotFound):
        await lifecycle.delete(product_ref())


async def test_a_429_is_retried_and_then_succeeds(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.delete(DELETE_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(204),
        ]
    )
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.DELETE}))

    result = await lifecycle.delete(product_ref())

    assert len(respx_mock.calls) == 3
    assert result is None


async def test_a_5xx_that_outlives_retries_raises_transient_error(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.delete(DELETE_URL).mock(return_value=httpx.Response(503))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.DELETE}))

    with pytest.raises(TransientError) as excinfo:
        await lifecycle.delete(product_ref())

    assert excinfo.value.retryable is True
    assert len(respx_mock.calls) == 3


async def test_a_transport_failure_raises_transient_error(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.delete(DELETE_URL).mock(side_effect=httpx.ConnectError("no route"))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.DELETE}))

    with pytest.raises(TransientError):
        await lifecycle.delete(product_ref())
