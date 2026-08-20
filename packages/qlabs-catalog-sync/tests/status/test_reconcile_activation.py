""":func:`~qlabs_catalog_sync.sync.status.reconcile_activation` against a real (fake)
connector.

Every scenario the task's DoD names is proven here against
:class:`~qlabs_catalog_sync_sdk.testing.FakeConnector`'s real, in-memory ``call_log`` --
never a mock -- across both a create-path and an update-path target ref, using the *same*
:func:`~qlabs_catalog_sync.sync.status.reconcile_activation` call either way (the
"consistent rather than duplicated" requirement: there is exactly one function, not a
create-flavored one and an update-flavored one).
"""

from __future__ import annotations

from status_helpers import ActivatingFakeConnector, data_product, make_pair

from qlabs_catalog_sync.sync.status import ActivationIntent, reconcile_activation
from qlabs_catalog_sync_sdk.contract import IdentityRef
from qlabs_catalog_sync_sdk.models import DataProductStatus, EntityType, FieldChange, FieldDiff
from qlabs_catalog_sync_sdk.testing import FakeConnector

MANAGED_SPACE = "managed-space-1"


async def _create_path_ref(
    target: FakeConnector, *, status: DataProductStatus | None
) -> IdentityRef:
    """A target ref reached the way ``loop.py``'s create path reaches one: a fresh
    ``create()`` call, whose returned ref is what ``reconcile_activation`` is then handed
    (module docstring, wiring note 3 -- create and update feed the same function)."""
    result = await target.create(data_product("orders", status=status))
    return result.ref


async def _update_path_ref(
    target: FakeConnector, *, status: DataProductStatus | None
) -> IdentityRef:
    """A target ref reached the way ``loop.py``'s update path reaches one: the object
    already exists (seeded, bypassing the capability gate exactly like an earlier cycle's
    create would have), and an ordinary field write touches it first."""
    ref = target.seed(data_product("orders", status=status), native_key="qlik-orders")
    await target.update(
        ref,
        FieldDiff(
            entity_type=EntityType.DATA_PRODUCT,
            changes=[FieldChange(field="description", value={"text": "updated", "kind": "plain"})],
        ),
    )
    return ref


# -- opt-in off: the DoD's hard half, across both paths --------------------------------------


async def test_opt_in_off_never_requests_activation_on_the_create_path() -> None:
    target = ActivatingFakeConnector.write_target()
    ref = await _create_path_ref(target, status=DataProductStatus.ACTIVE)
    before = target.call_count()

    outcome = await reconcile_activation(
        target,
        ref,
        data_product("orders", status=DataProductStatus.ACTIVE),
        pair=make_pair(activation_opt_in=False),
        managed_space_id=MANAGED_SPACE,
    )

    assert outcome.attempted is False
    assert outcome.decision.requested is False
    # No call reached the connector at all -- not "a call without status", *no call*.
    assert target.call_count() == before


async def test_opt_in_off_never_requests_activation_on_the_update_path() -> None:
    target = ActivatingFakeConnector.write_target()
    ref = await _update_path_ref(target, status=DataProductStatus.ACTIVE)
    before = target.call_count()

    outcome = await reconcile_activation(
        target,
        ref,
        data_product("orders", status=DataProductStatus.ACTIVE),
        pair=make_pair(activation_opt_in=False),
        managed_space_id=MANAGED_SPACE,
    )

    assert outcome.attempted is False
    assert target.call_count() == before


# -- opt-in on, every condition satisfied: activation is actually requested -------------------


async def test_opt_in_on_managed_space_and_active_status_requests_activation() -> None:
    target = ActivatingFakeConnector.write_target()
    ref = await _create_path_ref(target, status=None)  # create never carries status (loop.py)
    updates_before = target.call_count("update")

    outcome = await reconcile_activation(
        target,
        ref,
        data_product("orders", status=DataProductStatus.ACTIVE),
        pair=make_pair(activation_opt_in=True),
        managed_space_id=MANAGED_SPACE,
    )

    assert outcome.decision.intent is ActivationIntent.ACTIVATE
    assert outcome.attempted is True
    assert outcome.unsupported is False
    assert outcome.write_result is not None
    # The request really reached the connector's call log, via its activation route.
    assert target.call_count("update") == updates_before + 1
    sent: FieldDiff = target.calls("update")[-1].args["diff"]
    change = sent.change_for("status")
    assert change is not None
    assert change.value == DataProductStatus.ACTIVE.value


async def test_opt_in_on_works_the_same_way_on_the_update_path() -> None:
    """Same assertions as the create-path test above, over the update-path ref -- proving
    the one function behaves identically regardless of how the ref was reached."""
    target = ActivatingFakeConnector.write_target()
    ref = await _update_path_ref(target, status=None)
    updates_before = target.call_count("update")

    outcome = await reconcile_activation(
        target,
        ref,
        data_product("orders", status=DataProductStatus.ACTIVE),
        pair=make_pair(activation_opt_in=True),
        managed_space_id=MANAGED_SPACE,
    )

    assert outcome.attempted is True
    assert target.call_count("update") == updates_before + 1


# -- managed space only ------------------------------------------------------------------------


async def test_a_non_managed_space_is_never_activated_even_when_opted_in() -> None:
    target = ActivatingFakeConnector.write_target()
    ref = await _create_path_ref(target, status=None)
    before = target.call_count()

    outcome = await reconcile_activation(
        target,
        ref,
        data_product("orders", status=DataProductStatus.ACTIVE),
        pair=make_pair(activation_opt_in=True),
        managed_space_id=None,
    )

    assert outcome.attempted is False
    assert outcome.unsupported is False
    assert target.call_count() == before


# -- deactivation: never requested, asserted directly against a connector already active ------


async def test_an_object_no_longer_active_is_never_deactivated() -> None:
    """The object is genuinely activated at the target already (seeded with
    ``status=ACTIVE``); the *source* now reports a different status. Decision (module
    docstring, "whether v1 deactivates"): nothing is requested, and the target's own stored
    state is left completely alone -- proven by reading it back unchanged."""
    target = ActivatingFakeConnector.write_target()
    seeded = data_product("orders", status=DataProductStatus.ACTIVE)
    ref = target.seed(seeded, native_key="qlik-orders")
    before = target.call_count()

    outcome = await reconcile_activation(
        target,
        ref,
        data_product("orders", status=DataProductStatus.DEPRECATED),
        pair=make_pair(activation_opt_in=True),
        managed_space_id=MANAGED_SPACE,
    )

    assert outcome.decision.intent is ActivationIntent.NONE
    assert outcome.attempted is False
    assert target.call_count() == before
    stored = await target.read(ref)
    assert stored.status is DataProductStatus.ACTIVE  # untouched


# -- the connector genuinely cannot carry this out today -----------------------------------


async def test_a_connector_with_no_activation_route_is_reported_as_unsupported_not_called() -> None:
    """The real, current shape of every connector this repository ships (module docstring,
    wiring note 1): D7 says activate, but there is no generic route to ask for it, so
    nothing is called and that fact is reported rather than silently swallowed."""
    target = FakeConnector.write_target()  # the plain fixture -- no ``activate`` route
    ref = await _create_path_ref(target, status=None)
    before = target.call_count()

    outcome = await reconcile_activation(
        target,
        ref,
        data_product("orders", status=DataProductStatus.ACTIVE),
        pair=make_pair(activation_opt_in=True),
        managed_space_id=MANAGED_SPACE,
    )

    assert outcome.decision.requested is True  # D7 itself says yes
    assert outcome.attempted is False  # but nothing could carry it out
    assert outcome.unsupported is True
    assert target.call_count() == before
