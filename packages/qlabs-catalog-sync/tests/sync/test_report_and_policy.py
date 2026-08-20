"""The run report as a value, and the manual-edit seam T2.5 plugs into.

T2.8 serializes the report as its machine-readable dry-run plan and T8.1 asserts the pilot
against it, so its shape is a contract, not an implementation detail.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from sync_helpers import bind, data_product, seed_product

from qlabs_catalog_sync.config import SyncPairConfig
from qlabs_catalog_sync.state.store import StateStore
from qlabs_catalog_sync.sync.loop import (
    ACTIVATION_FIELD,
    RecordOutcome,
    RunStatus,
    SkipReason,
    SyncLoop,
    WriteReview,
)
from qlabs_catalog_sync_sdk.models import DataProductStatus, EntityType
from qlabs_catalog_sync_sdk.testing import FakeConnector


class HoldDescription:
    """A stand-in for T2.5: a policy that refuses to overwrite one hand-edited field."""

    def __init__(self, *fields: str, reason: str = "manual_edit_preserved") -> None:
        self.fields = fields
        self.reason = reason
        self.reviews: list[WriteReview] = []

    async def withhold(self, review: WriteReview) -> Mapping[str, str]:
        self.reviews.append(review)
        return {name: self.reason for name in self.fields}


class WriteEverything:
    """A policy that tries to withhold nothing at all -- including activation."""

    async def withhold(self, review: WriteReview) -> Mapping[str, str]:
        return {}


async def test_the_report_serializes_to_plain_json(
    make_loop: Callable[..., SyncLoop], source: FakeConnector
) -> None:
    """``to_json`` is the dry-run plan file, so it has to be genuinely serializable."""
    seed_product(source, "sales.orders", description="Order facts", tags=[("tier", "gold")])
    seed_product(source, "hr.people")

    report = await make_loop(create_missing=True, dry_run=True).run_cycle(EntityType.DATA_PRODUCT)
    payload = json.loads(json.dumps(report.to_json()))

    assert payload["pair"] == "db-to-qlik"
    assert payload["source_endpoint"] == source.name
    assert payload["entity_type"] == "data_product"
    assert payload["dry_run"] is True
    assert payload["committed"] is False
    assert payload["counts"]["created"] == 1
    assert payload["counts"]["filtered"] == 1
    assert {record["native_key"] for record in payload["records"]} == {
        "sales.orders",
        "hr.people",
    }
    assert payload["watermark"]["advanced"] is False


async def test_the_report_names_every_field_that_did_not_make_it_across(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """Dropped by the manifest, withheld by policy, or skipped by the target: all reported."""
    source_ref = seed_product(
        source, "sales.orders", description="Order facts", status=DataProductStatus.ACTIVE
    )
    existing = target.seed(data_product("orders", description="stale"), native_key="qlik-orders")
    await bind(store, source_ref, existing)

    report = await make_loop().run_cycle(EntityType.DATA_PRODUCT)
    record = report.records[0]

    # The target's manifest cannot carry these two at all, and says which kind of "cannot".
    dropped = {item.field: item.reason.value for _, item in report.dropped_fields}
    assert dropped == {"glossary_term_refs": "not_applicable", "placement": "read_only"}
    # Engine policy held this one back (decision D7).
    withheld = {item.field: item.reason for _, item in report.withheld_fields}
    assert withheld == {ACTIVATION_FIELD: "activation_not_opted_in"}
    # And the target reported back exactly which of the fields it was sent it did not
    # write -- the channel decisions D2 and D3 report unresolved references through.
    assert record.written_fields == ("description",)
    assert set(record.target_skipped_fields) == {
        "name",
        "documentation",
        "owners",
        "tags",
        "dataset_refs",
    }

    payload = report.to_json()
    serialized = payload["records"][0]
    assert {entry["field"] for entry in serialized["dropped"]} == set(dropped)
    assert {entry["field"] for entry in serialized["withheld"]} == set(withheld)
    assert set(serialized["target_skipped_fields"]) == set(record.target_skipped_fields)


async def test_the_report_counts_each_outcome(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """One cycle, four different fates, all counted."""
    settled = seed_product(source, "sales.settled", description="already in sync")
    target.seed(data_product("settled", description="already in sync"), native_key="qlik-settled")
    await bind(store, settled, target.seed(data_product("x"), native_key="qlik-noop"))
    seed_product(source, "sales.new")  # no counterpart, creation off -> skipped
    seed_product(source, "hr.people")  # outside the selector -> filtered

    report = await make_loop().run_cycle(EntityType.DATA_PRODUCT)

    assert report.count(RecordOutcome.FILTERED) == 1
    assert report.count(RecordOutcome.SKIPPED) >= 1
    assert report.read_count == 2  # the filtered one was never read
    assert report.records_with(SkipReason.NO_TARGET_BINDING)[0].native_key == "sales.new"
    assert report.status is RunStatus.PARTIAL
    assert report.ok is False


async def test_an_injected_policy_can_hold_a_field_back(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """The T2.5 seam: a policy names fields to withhold and the loop honors it."""
    source_ref = seed_product(source, "sales.orders", description="Order facts")
    existing = target.seed(
        data_product("stale name", description="stale"), native_key="qlik-orders"
    )
    await bind(store, source_ref, existing)
    policy = HoldDescription("description")

    report = await make_loop(write_policy=policy).run_cycle(EntityType.DATA_PRODUCT)

    record = report.records[0]
    assert record.outcome is RecordOutcome.WRITTEN
    assert "description" not in record.written_fields
    assert "name" in record.written_fields
    assert {item.field: item.reason for item in record.withheld} == {
        "description": "manual_edit_preserved",
        ACTIVATION_FIELD: "activation_not_opted_in",
    }

    # The policy really was handed the plan and the last-known target state to judge with.
    assert len(policy.reviews) == 1
    review = policy.reviews[0]
    assert review.pair.name == "db-to-qlik"
    assert review.target_ref.native_key == "qlik-orders"
    assert review.plan.change_for("description") is not None
    assert review.target is target


async def test_a_withheld_field_is_not_persisted_so_it_stays_visible(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """Persisting a value the target never received would be a lie the next cycle believes."""
    source_ref = seed_product(source, "sales.orders", description="Order facts")
    existing = target.seed(
        data_product("stale name", description="stale"), native_key="qlik-orders"
    )
    await bind(store, source_ref, existing)
    loop = make_loop(write_policy=HoldDescription("description"))

    first = await loop.run_cycle(EntityType.DATA_PRODUCT)
    neutral_id = first.records[0].neutral_id
    assert neutral_id is not None
    envelopes = await store.fetch_envelopes(neutral_id, target.name)
    assert "description" not in envelopes

    # A withheld field is a standing decision, not outstanding work, so it does not pin
    # the watermark. The next time the object genuinely changes it is diffed -- and
    # reported -- again, because nothing was ever persisted for it.
    source.simulate_external_edit(source_ref, {"name": "renamed"})
    second = await loop.run_cycle(EntityType.DATA_PRODUCT)
    assert "description" in second.records[0].changed_fields
    assert "description" not in second.records[0].written_fields
    assert "description" not in await store.fetch_envelopes(neutral_id, target.name)


async def test_a_policy_cannot_re_enable_activation(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    store: StateStore,
) -> None:
    """D7 is applied before any policy, so a permissive policy cannot undo it."""
    source_ref = seed_product(
        source, "sales.orders", description="Order facts", status=DataProductStatus.ACTIVE
    )
    existing = target.seed(data_product("orders", description="stale"), native_key="qlik-orders")
    await bind(store, source_ref, existing)

    report = await make_loop(write_policy=WriteEverything()).run_cycle(EntityType.DATA_PRODUCT)

    assert [item.field for item in report.records[0].withheld] == [ACTIVATION_FIELD]
    assert ACTIVATION_FIELD not in target.calls("update")[0].args["diff"].field_names


async def test_a_create_never_carries_a_status_the_pair_did_not_opt_into(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    pair: SyncPairConfig,
) -> None:
    """D7 on the create path too: the engine does not tell a new product to be active."""
    seed_product(source, "sales.orders", status=DataProductStatus.ACTIVE)

    report = await make_loop(create_missing=True).run_cycle(EntityType.DATA_PRODUCT)

    created = target.calls("create")[0].args["entity"]
    assert created.status is None
    assert [item.field for item in report.records[0].withheld] == [ACTIVATION_FIELD]


async def test_a_create_carries_the_status_once_the_pair_opts_in(
    make_loop: Callable[..., SyncLoop],
    source: FakeConnector,
    target: FakeConnector,
    pair: SyncPairConfig,
) -> None:
    """The same create, with the pair's explicit opt-in, does carry the lifecycle state."""
    seed_product(source, "sales.orders", status=DataProductStatus.ACTIVE)
    opted_in = pair.model_copy(update={"activation_opt_in": True})

    report = await make_loop(pair=opted_in, create_missing=True).run_cycle(
        EntityType.DATA_PRODUCT
    )

    assert target.calls("create")[0].args["entity"].status is DataProductStatus.ACTIVE
    assert report.records[0].withheld == ()
