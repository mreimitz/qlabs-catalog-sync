"""Cross-cutting opt-in guarantees: what a default-constructed :class:`LifecycleActions`
refuses, and the entity-type guard shared by all four actions.

The per-action, per-endpoint version of "refused without the opt-in, zero requests" lives
alongside each action's own test module (``test_activate.py`` etc.); this module proves
the guarantee that matters across all of them at once — default construction disables
*everything*, and naming one action never leaks into another.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_connector_qlik.lifecycle import (
    ALL_DESTRUCTIVE_ACTIONS,
    DestructiveAction,
    LifecycleActions,
)

from .conftest import DATA_PRODUCTS_URL, MANAGED_SPACE_ID, PRODUCT_ID, dataset_ref, product_ref

ACTIVATE_URL = f"{DATA_PRODUCTS_URL}/{PRODUCT_ID}/actions/activate"
DEACTIVATE_URL = f"{DATA_PRODUCTS_URL}/{PRODUCT_ID}/actions/deactivate"
MOVE_URL = f"{DATA_PRODUCTS_URL}/{PRODUCT_ID}/actions/move"
DELETE_URL = f"{DATA_PRODUCTS_URL}/{PRODUCT_ID}"


async def test_default_construction_enables_nothing(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    """A plain ``LifecycleActions(http, endpoint=...)`` — the shape a call site gets if it
    forgets to think about the opt-in at all — refuses every one of the four actions and
    issues zero HTTP requests across all of them."""
    respx_mock.post(ACTIVATE_URL).mock(return_value=httpx.Response(200, json={"activated": True}))
    respx_mock.post(DEACTIVATE_URL).mock(return_value=httpx.Response(200, json={}))
    respx_mock.post(MOVE_URL).mock(return_value=httpx.Response(204))
    respx_mock.delete(DELETE_URL).mock(return_value=httpx.Response(204))
    lifecycle = make_lifecycle()

    assert lifecycle.enabled_actions == frozenset()

    with pytest.raises(CapabilityError):
        await lifecycle.activate(product_ref(), name="x", managed_space_id=MANAGED_SPACE_ID)
    with pytest.raises(CapabilityError):
        await lifecycle.deactivate(product_ref())
    with pytest.raises(CapabilityError):
        await lifecycle.move(product_ref(), target_space_id="some-space")
    with pytest.raises(CapabilityError):
        await lifecycle.delete(product_ref())

    assert len(respx_mock.calls) == 0


@pytest.mark.parametrize("action", list(DestructiveAction))
async def test_naming_one_action_enables_only_that_action(
    action: DestructiveAction,
    respx_mock: object,
    make_lifecycle: Callable[..., LifecycleActions],
) -> None:
    """Opting into exactly one :class:`DestructiveAction` must never widen to the other
    three — a per-pair config that turns on activation (D7) has no way to also turn on
    delete as a side effect."""
    respx_mock.post(ACTIVATE_URL).mock(return_value=httpx.Response(200, json={"activated": True}))
    respx_mock.post(DEACTIVATE_URL).mock(return_value=httpx.Response(200, json={}))
    respx_mock.post(MOVE_URL).mock(return_value=httpx.Response(204))
    respx_mock.delete(DELETE_URL).mock(return_value=httpx.Response(204))
    lifecycle = make_lifecycle(enabled_actions=frozenset({action}))

    calls = {
        DestructiveAction.ACTIVATE: lambda: lifecycle.activate(
            product_ref(), name="x", managed_space_id=MANAGED_SPACE_ID
        ),
        DestructiveAction.DEACTIVATE: lambda: lifecycle.deactivate(product_ref()),
        DestructiveAction.MOVE: lambda: lifecycle.move(product_ref(), target_space_id="some-space"),
        DestructiveAction.DELETE: lambda: lifecycle.delete(product_ref()),
    }

    for other_action, call in calls.items():
        if other_action is action:
            continue
        with pytest.raises(CapabilityError):
            await call()

    # The named action, and only it, actually reaches the network.
    await calls[action]()
    assert len(respx_mock.calls) == 1


async def test_enabling_every_action_is_a_deliberate_named_choice(
    respx_mock: object, make_lifecycle: Callable[..., LifecycleActions]
) -> None:
    """``ALL_DESTRUCTIVE_ACTIONS`` exists for the one caller that really means "every
    action" (a live-tenant probe script, or this test) — using it is still an explicit,
    visible construction-time argument, never a default."""
    respx_mock.delete(DELETE_URL).mock(return_value=httpx.Response(204))
    lifecycle = make_lifecycle(enabled_actions=ALL_DESTRUCTIVE_ACTIONS)

    assert lifecycle.enabled_actions == frozenset(DestructiveAction)
    assert await lifecycle.delete(product_ref()) is None


@pytest.mark.parametrize(
    "call",
    [
        lambda lifecycle: lifecycle.activate(
            dataset_ref(), name="x", managed_space_id=MANAGED_SPACE_ID
        ),
        lambda lifecycle: lifecycle.deactivate(dataset_ref()),
        lambda lifecycle: lifecycle.move(dataset_ref(), target_space_id="some-space"),
        lambda lifecycle: lifecycle.delete(dataset_ref()),
    ],
)
async def test_a_non_data_product_ref_is_refused_before_any_request(
    call: Callable[[LifecycleActions], object],
    respx_mock: object,
    make_lifecycle: Callable[..., LifecycleActions],
) -> None:
    """Qlik's data-governance actions and ``DELETE`` are data-product-only endpoints; a
    ref for any other entity type is refused even with every action enabled."""
    lifecycle = make_lifecycle(enabled_actions=ALL_DESTRUCTIVE_ACTIONS)

    with pytest.raises(CapabilityError):
        await call(lifecycle)

    assert len(respx_mock.calls) == 0
