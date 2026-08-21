"""``LifecycleActions.activate`` — the documented request, the opt-in gate, error
classification, and the non-managed-space failure mode.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import WriteOutcome
from qlabs_catalog_sync_sdk.exceptions import (
    AuthError,
    CapabilityError,
    ConnectorError,
    TransientError,
)
from qlabs_connector_qlik.lifecycle import DestructiveAction, LifecycleActions

from .conftest import (
    DATA_PRODUCTS_URL,
    MANAGED_SPACE_ID,
    PRODUCT_ID,
    activate_response,
    product_ref,
    sent_body,
)

ACTIVATE_URL = f"{DATA_PRODUCTS_URL}/{PRODUCT_ID}/actions/activate"


async def test_activate_issues_the_documented_request(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(ACTIVATE_URL).mock(return_value=httpx.Response(200, json=activate_response()))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.ACTIVATE}))
    ref = product_ref()

    result = await lifecycle.activate(
        ref, name="Sales Analytics Data Product", managed_space_id=MANAGED_SPACE_ID
    )

    request = respx_mock.calls.last.request
    assert request.method == "POST"
    assert request.url.path == f"/api/data-governance/data-products/{PRODUCT_ID}/actions/activate"
    assert sent_body(respx_mock) == {
        "name": "Sales Analytics Data Product",
        "spaceId": MANAGED_SPACE_ID,
    }
    assert result.outcome is WriteOutcome.UPDATED
    assert result.written_fields == ["status"]
    # Activation never changes identity — the returned ref is the one that was passed in.
    assert result.ref == ref


async def test_activate_is_refused_without_the_opt_in_and_issues_zero_requests(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(ACTIVATE_URL).mock(return_value=httpx.Response(200, json=activate_response()))
    lifecycle = make_lifecycle()  # default: nothing enabled

    with pytest.raises(CapabilityError) as excinfo:
        await lifecycle.activate(
            product_ref(), name="Sales Analytics Data Product", managed_space_id=MANAGED_SPACE_ID
        )

    assert excinfo.value.operation == "activate"
    assert excinfo.value.retryable is False
    assert len(respx_mock.calls) == 0


async def test_a_403_raises_auth_error(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(ACTIVATE_URL).mock(
        return_value=httpx.Response(403, json={"errors": [{"code": "FORBIDDEN"}]})
    )
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.ACTIVATE}))

    with pytest.raises(AuthError) as excinfo:
        await lifecycle.activate(
            product_ref(), name="Sales Analytics Data Product", managed_space_id=MANAGED_SPACE_ID
        )

    assert excinfo.value.retryable is False
    assert len(respx_mock.calls) == 1


async def test_a_429_is_retried_and_then_succeeds(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(ACTIVATE_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json=activate_response()),
        ]
    )
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.ACTIVATE}))

    result = await lifecycle.activate(
        product_ref(), name="Sales Analytics Data Product", managed_space_id=MANAGED_SPACE_ID
    )

    assert len(respx_mock.calls) == 3
    assert result.outcome is WriteOutcome.UPDATED


async def test_a_5xx_that_outlives_retries_raises_transient_error(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    respx_mock.post(ACTIVATE_URL).mock(return_value=httpx.Response(503))
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.ACTIVATE}))

    with pytest.raises(TransientError) as excinfo:
        await lifecycle.activate(
            product_ref(), name="Sales Analytics Data Product", managed_space_id=MANAGED_SPACE_ID
        )

    assert excinfo.value.retryable is True
    assert len(respx_mock.calls) == 3  # the `make_http` fixture caps attempts at 3


async def test_activation_against_a_non_managed_space_fails_clearly(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    """RS-02 documents activation as managed-space-only but not the exact failure status
    (TENANT_UNVERIFIED). An unclassified 4xx (not 401/403/404/429) from this endpoint is
    treated as very likely that precondition, and the message says so."""
    respx_mock.post(ACTIVATE_URL).mock(
        return_value=httpx.Response(400, json={"errors": [{"code": "SPACE_NOT_MANAGED"}]})
    )
    lifecycle = make_lifecycle(enabled_actions=frozenset({DestructiveAction.ACTIVATE}))

    with pytest.raises(ConnectorError) as excinfo:
        await lifecycle.activate(
            product_ref(), name="Sales Analytics Data Product", managed_space_id=MANAGED_SPACE_ID
        )

    # Not one of the specifically-typed exceptions — a plain classified ConnectorError,
    # but with an explanation, not just "HTTP 400".
    assert type(excinfo.value) is ConnectorError
    assert "managed" in str(excinfo.value).lower()
    assert MANAGED_SPACE_ID in str(excinfo.value)
    assert len(respx_mock.calls) == 1
