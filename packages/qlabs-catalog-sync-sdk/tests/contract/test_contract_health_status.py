"""A red health status quarantines one endpoint, so it has to carry why."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from qlabs_catalog_sync_sdk.contract import HealthState, HealthStatus

CHECKED_AT = datetime(2026, 8, 20, 9, 30, 0, tzinfo=UTC)


def test_healthy_needs_no_reason() -> None:
    status = HealthStatus.healthy("qlik", checked_at=CHECKED_AT, details={"latencyMs": 42})

    assert status.state is HealthState.HEALTHY
    assert status.is_healthy
    assert not status.should_quarantine
    assert status.reason is None
    assert status.details == {"latencyMs": 42}


def test_degraded_is_impaired_but_still_scheduled() -> None:
    status = HealthStatus.degraded("qlik", "rate limited, backing off")

    assert status.state is HealthState.DEGRADED
    assert not status.is_healthy
    assert not status.should_quarantine
    assert status.reason == "rate limited, backing off"


def test_unhealthy_quarantines_the_endpoint() -> None:
    status = HealthStatus.unhealthy("databricks", "401 from the token endpoint")

    assert status.should_quarantine
    assert status.reason == "401 from the token endpoint"


@pytest.mark.parametrize("state", [HealthState.DEGRADED, HealthState.UNHEALTHY])
def test_a_non_healthy_status_must_explain_itself(state: HealthState) -> None:
    with pytest.raises(ValidationError, match="must carry a reason"):
        HealthStatus(endpoint="qlik", state=state)

    with pytest.raises(ValidationError, match="must carry a reason"):
        HealthStatus(endpoint="qlik", state=state, reason="")


def test_an_empty_endpoint_is_rejected() -> None:
    with pytest.raises(ValidationError):
        HealthStatus(endpoint="", state=HealthState.HEALTHY)


def test_the_status_round_trips() -> None:
    status = HealthStatus.unhealthy(
        "databricks",
        "warehouse unreachable",
        checked_at=CHECKED_AT,
        details={"warehouseId": "wh-1", "attempts": 3},
    )

    assert HealthStatus.model_validate(status.model_dump(mode="json")) == status
    assert HealthStatus.model_validate(status.model_dump(mode="json", by_alias=True)) == status
