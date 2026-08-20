"""Failure injection: a test can make the next call to a given method (or the Nth) raise
a chosen SDK exception, so the engine's retry, quarantine and conflict paths are
testable without a real flaky endpoint.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.contract import Watermark
from qlabs_catalog_sync_sdk.exceptions import AuthError, ConflictError, TransientError
from qlabs_catalog_sync_sdk.models import DataProduct, EntityType, FieldChange, FieldDiff
from qlabs_catalog_sync_sdk.testing import FakeConnector


async def test_fail_next_raises_the_injected_exception_type(target: FakeConnector) -> None:
    target.fail_next("healthcheck", TransientError("simulated 503"))

    with pytest.raises(TransientError, match="simulated 503"):
        await target.healthcheck()


async def test_after_the_injected_failure_the_call_behaves_normally_again(
    target: FakeConnector,
) -> None:
    target.fail_next("healthcheck", TransientError("simulated 503"))

    with pytest.raises(TransientError):
        await target.healthcheck()
    status = await target.healthcheck()

    assert status.is_healthy


async def test_a_failed_call_is_still_recorded_on_the_call_log(target: FakeConnector) -> None:
    target.fail_next("healthcheck", AuthError("simulated 401"))

    with pytest.raises(AuthError):
        await target.healthcheck()

    assert target.call_count("healthcheck") == 1
    assert target.call_log[0].result is None  # never completed


async def test_a_failed_call_never_partially_applies(target: FakeConnector) -> None:
    """create() fails before the object is ever stored."""
    target.fail_next("create", TransientError("simulated 503"))

    with pytest.raises(TransientError):
        await target.create(DataProduct(name="Retail Sales"))

    watermark = Watermark.initial(target.name, EntityType.DATA_PRODUCT)
    result = await target.list_changed(EntityType.DATA_PRODUCT, watermark)
    assert result.is_empty


async def test_fail_next_is_fifo_and_supports_failing_the_nth_call(target: FakeConnector) -> None:
    target.fail_next("healthcheck", TransientError("first failure"))
    target.fail_next("healthcheck", TransientError("second failure"))

    with pytest.raises(TransientError, match="first failure"):
        await target.healthcheck()
    with pytest.raises(TransientError, match="second failure"):
        await target.healthcheck()
    status = await target.healthcheck()  # third call: queue is empty, succeeds
    assert status.is_healthy


async def test_failing_specifically_the_nth_call(target: FakeConnector) -> None:
    """To fail exactly the Nth call: let N - 1 calls happen, then queue the failure
    immediately before the Nth."""
    await target.healthcheck()
    await target.healthcheck()
    target.fail_next("healthcheck", TransientError("boom"))

    with pytest.raises(TransientError, match="boom"):
        await target.healthcheck()  # this is the 3rd call

    assert target.call_count("healthcheck") == 3


async def test_injected_conflict_error_surfaces_with_its_own_type(target: FakeConnector) -> None:
    created = await target.create(DataProduct(name="Retail Sales"))
    target.fail_next(
        "update",
        ConflictError("simulated 412", expected_revision="rev-1", actual_revision="rev-9"),
    )

    diff = FieldDiff(
        entity_type=EntityType.DATA_PRODUCT, changes=[FieldChange(field="name", value="x")]
    )
    with pytest.raises(ConflictError) as excinfo:
        await target.update(created.ref, diff)

    assert excinfo.value.actual_revision == "rev-9"


async def test_failures_for_different_methods_do_not_interfere(target: FakeConnector) -> None:
    target.fail_next("read", TransientError("read is down"))

    status = await target.healthcheck()  # unaffected

    assert status.is_healthy
    created = await target.create(DataProduct(name="Retail Sales"))
    with pytest.raises(TransientError, match="read is down"):
        await target.read(created.ref)
