"""Every contract method call is recorded in order with its arguments, is easy to filter
and count, and easy to reset — the mechanism an engine test uses to assert something
like "the sync loop performed no writes on a re-run".
"""

from __future__ import annotations

from qlabs_catalog_sync_sdk.contract import Watermark, WriteOutcome
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType, FieldChange, FieldDiff
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_calls_are_recorded_in_order(target: FakeConnector) -> None:
    await target.healthcheck()
    created = await target.create(DataProduct(name="Retail Sales"))
    await target.read(created.ref)

    assert [entry.method for entry in target.call_log] == ["healthcheck", "create", "read"]


async def test_call_args_are_captured(target: FakeConnector) -> None:
    created = await target.create(DataProduct(name="Retail Sales"))

    entry = target.call_log[0]
    assert entry.method == "create"
    assert entry.args["entity"].name == "Retail Sales"
    assert entry.result is created


async def test_calls_filters_to_one_method(target: FakeConnector) -> None:
    await target.healthcheck()
    await target.create(DataProduct(name="A"))
    await target.healthcheck()

    assert target.call_count("healthcheck") == 2
    assert target.call_count("create") == 1
    assert target.call_count() == 3
    assert len(target.calls("healthcheck")) == 2


async def test_reset_call_log_clears_history_without_touching_the_store(
    target: FakeConnector,
) -> None:
    created = await target.create(DataProduct(name="Retail Sales"))

    target.reset_call_log()

    assert target.call_log == []
    # The store itself is untouched — a subsequent read still finds the object.
    entity = await target.read(created.ref)
    assert entity.name == "Retail Sales"
    assert target.call_count("read") == 1  # only the post-reset read is recorded


async def test_asserting_no_writes_happened_on_a_re_run(target: FakeConnector) -> None:
    """The exact pattern an engine idempotency test uses: run a cycle, reset the log,
    run again with no real changes, and assert zero writes."""
    created = await target.create(DataProduct(name="Retail Sales"))
    target.reset_call_log()

    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT,
        changes=[FieldChange(field="name", value="Retail Sales")],
    )
    result = await target.update(created.ref, diff)

    assert result.outcome is WriteOutcome.NO_OP
    assert target.call_count("create") == 0
    assert all(entry.result.outcome is WriteOutcome.NO_OP for entry in target.calls("update"))


async def test_list_changed_call_args_are_captured(source: FakeConnector) -> None:
    watermark = Watermark.initial(source.name, EntityType.DATA_PRODUCT)

    await source.list_changed(EntityType.DATA_PRODUCT, watermark)

    entry = source.call_log[0]
    assert entry.args["entity_type"] is EntityType.DATA_PRODUCT
    assert entry.args["since"] == watermark
