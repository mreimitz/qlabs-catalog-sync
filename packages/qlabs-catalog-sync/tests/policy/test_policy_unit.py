"""Unit-level tests of :class:`ManualEditWritePolicy.withhold` against genuine reviews.

Each :class:`~qlabs_catalog_sync.sync.loop.WriteReview` is built by
``policy_helpers.build_review``, which runs the real
:func:`~qlabs_catalog_sync.diff.compute_field_diff` (T7.2) against a real
:class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` target -- the exact same seam
``sync/loop.py`` itself calls through -- so these tests exercise the policy precisely as
the loop would, without paying for a whole sync cycle around every case. The full-loop
behavior (a real cycle, a real commit, a real re-run) is covered separately in
``test_loop_integration.py``.

``description`` is a :class:`~qlabs_catalog_sync_sdk.models.TextField`, not a bare
string -- every value assigned to it below goes through :func:`TextField.plain` so a
:class:`~qlabs_catalog_sync_sdk.testing.FakeConnector` re-``read`` of it still validates,
exactly as a real connector's field would.
"""

from __future__ import annotations

from policy_helpers import build_review, data_product

from qlabs_catalog_sync.config import ManualEditMode, ManualEditPolicy, SyncPairConfig
from qlabs_catalog_sync.sync.policy import MANUAL_EDIT_WITHHELD_REASON, ManualEditWritePolicy
from qlabs_catalog_sync_sdk.exceptions import TransientError
from qlabs_catalog_sync_sdk.models import EntityType, TextField
from qlabs_catalog_sync_sdk.testing import FakeConnector


def _with_policy(pair: SyncPairConfig, policy: ManualEditPolicy) -> SyncPairConfig:
    return pair.model_copy(update={"manual_edit_policy": policy})


async def test_source_wins_default_never_rereads_the_target(
    pair: SyncPairConfig, target: FakeConnector
) -> None:
    """v1's default, with no configuration at all: source wins regardless of whether the
    field was hand-edited, so detecting the edit would not change the outcome -- and this
    policy never spends the read proving something it would not act on."""
    ref = target.seed(
        data_product("orders", description="hand-edited by a human"), native_key="qlik-orders"
    )
    review = build_review(
        pair=pair,
        target=target,
        target_ref=ref,
        source_values={"description": TextField.plain("from the source")},
        stored_values={"description": TextField.plain("originally synced value")},
    )

    withheld = await ManualEditWritePolicy().withhold(review)

    assert withheld == {}
    assert target.call_count("read") == 0


async def test_a_field_the_target_never_had_is_not_a_manual_edit(
    pair: SyncPairConfig, target: FakeConnector
) -> None:
    """preserve_local, but the engine never recorded a synced value for this field: there
    is no baseline to contradict, so this is a first write, not an edit -- and
    ``stored_target_envelopes`` alone already answers that, with no read spent."""
    ref = target.seed(
        data_product("orders", description="pre-existing tenant data"), native_key="qlik-orders"
    )
    review = build_review(
        pair=_with_policy(pair, ManualEditPolicy(default=ManualEditMode.PRESERVE_LOCAL)),
        target=target,
        target_ref=ref,
        source_values={"description": TextField.plain("from the source")},
        stored_values=None,
    )

    withheld = await ManualEditWritePolicy().withhold(review)

    assert withheld == {}
    assert target.call_count("read") == 0


async def test_a_value_the_engine_itself_wrote_last_cycle_is_not_a_manual_edit(
    pair: SyncPairConfig, target: FakeConnector
) -> None:
    """preserve_local, a prior recorded value exists, and nothing touched the target
    since: the live read matches that baseline, so this is not an edit. Proving that
    costs a read -- a stored baseline exists, so it genuinely could have differed."""
    ref = target.seed(data_product("orders", description="synced value"), native_key="qlik-orders")
    review = build_review(
        pair=_with_policy(pair, ManualEditPolicy(default=ManualEditMode.PRESERVE_LOCAL)),
        target=target,
        target_ref=ref,
        source_values={"description": TextField.plain("a new source value")},
        stored_values={"description": TextField.plain("synced value")},
    )

    withheld = await ManualEditWritePolicy().withhold(review)

    assert withheld == {}
    assert target.call_count("read") == 1


async def test_preserve_local_detects_and_withholds_a_genuine_hand_edit(
    pair: SyncPairConfig, target: FakeConnector
) -> None:
    ref = target.seed(
        data_product("orders", description="originally synced value"), native_key="qlik-orders"
    )
    target.simulate_external_edit(
        ref, {"description": TextField.plain("a human changed this in Qlik")}
    )
    review = build_review(
        pair=_with_policy(pair, ManualEditPolicy(default=ManualEditMode.PRESERVE_LOCAL)),
        target=target,
        target_ref=ref,
        source_values={"description": TextField.plain("a new source value")},
        stored_values={"description": TextField.plain("originally synced value")},
    )

    withheld = await ManualEditWritePolicy().withhold(review)

    assert withheld == {"description": MANUAL_EDIT_WITHHELD_REASON}
    assert target.call_count("read") == 1


async def test_one_reread_covers_every_preserve_local_field_on_the_entity(
    pair: SyncPairConfig, target: FakeConnector
) -> None:
    """``name`` and ``description`` are both hand-edited; one entity under review, one
    read -- the read returns every field's live envelope together."""
    ref = target.seed(
        data_product("orders", description="originally synced value"), native_key="qlik-orders"
    )
    target.simulate_external_edit(
        ref, {"name": "renamed by hand", "description": TextField.plain("edited by hand")}
    )
    review = build_review(
        pair=_with_policy(pair, ManualEditPolicy(default=ManualEditMode.PRESERVE_LOCAL)),
        target=target,
        target_ref=ref,
        source_values={
            "name": "from source",
            "description": TextField.plain("from source"),
        },
        stored_values={
            "name": "orders",
            "description": TextField.plain("originally synced value"),
        },
    )

    withheld = await ManualEditWritePolicy().withhold(review)

    assert withheld == {
        "name": MANUAL_EDIT_WITHHELD_REASON,
        "description": MANUAL_EDIT_WITHHELD_REASON,
    }
    assert target.call_count("read") == 1


async def test_per_field_precedence_beats_per_entity_beats_default(
    pair: SyncPairConfig, target: FakeConnector
) -> None:
    """default source_wins, per_entity preserve_local, and a per_field entry back to
    source_wins for ``description``: the most specific entry wins, exactly as
    :meth:`~qlabs_catalog_sync.config.ManualEditPolicy.mode_for` documents."""
    ref = target.seed(
        data_product("orders", description="originally synced value"), native_key="qlik-orders"
    )
    target.simulate_external_edit(
        ref, {"name": "renamed by hand", "description": TextField.plain("edited by hand")}
    )
    policy = ManualEditPolicy(
        default=ManualEditMode.SOURCE_WINS,
        per_entity={EntityType.DATA_PRODUCT: ManualEditMode.PRESERVE_LOCAL},
        per_field={"data_product.description": ManualEditMode.SOURCE_WINS},
    )
    review = build_review(
        pair=_with_policy(pair, policy),
        target=target,
        target_ref=ref,
        source_values={
            "name": "from source",
            "description": TextField.plain("from source"),
        },
        stored_values={
            "name": "orders",
            "description": TextField.plain("originally synced value"),
        },
    )

    withheld = await ManualEditWritePolicy().withhold(review)

    # name: no per_field entry -> falls back to per_entity (preserve_local) -> detected.
    # description: the per_field entry wins over per_entity -> source_wins -> never
    # checked at all, and does not cost a read of its own (the shared one still fires
    # for `name`).
    assert withheld == {"name": MANUAL_EDIT_WITHHELD_REASON}
    assert target.call_count("read") == 1


async def test_a_failed_reread_fails_open_to_source_wins(
    pair: SyncPairConfig, target: FakeConnector
) -> None:
    """Detection needs a live read; when that read itself fails, this policy does not
    abort the cycle over it (an uncaught error here would fail the *entire* cycle, per
    ``sync/loop.py``'s outer handler) -- it defers to v1's source-wins default instead."""
    ref = target.seed(
        data_product("orders", description="originally synced value"), native_key="qlik-orders"
    )
    target.fail_next("read", TransientError("target briefly unreachable", endpoint=target.name))
    review = build_review(
        pair=_with_policy(pair, ManualEditPolicy(default=ManualEditMode.PRESERVE_LOCAL)),
        target=target,
        target_ref=ref,
        source_values={"description": TextField.plain("from the source")},
        stored_values={"description": TextField.plain("originally synced value")},
    )

    withheld = await ManualEditWritePolicy().withhold(review)

    assert withheld == {}
