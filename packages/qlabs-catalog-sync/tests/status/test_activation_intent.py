"""D7's pure mapping: neutral status -> :class:`~qlabs_catalog_sync.sync.status.ActivationIntent`.

"Each neutral status maps to the action you specified" (T7.4's DoD): every member of
:class:`~qlabs_catalog_sync_sdk.models.DataProductStatus`, plus the unset case, is covered
here individually so a future status added to the enum fails this test rather than silently
falling through to a default.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync.sync.status import ActivationIntent, activation_intent_for
from qlabs_catalog_sync_sdk.models import DataProductStatus


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (DataProductStatus.ACTIVE, ActivationIntent.ACTIVATE),
        (DataProductStatus.DRAFT, ActivationIntent.NONE),
        (DataProductStatus.DEPRECATED, ActivationIntent.NONE),
        (DataProductStatus.ARCHIVED, ActivationIntent.NONE),
        (None, ActivationIntent.NONE),
    ],
)
def test_activation_intent_for_every_status(
    status: DataProductStatus | None, expected: ActivationIntent
) -> None:
    assert activation_intent_for(status) is expected


def test_only_active_ever_activates() -> None:
    """Restated as one assertion over the whole enum, so a new status added later is
    exercised automatically rather than requiring someone to remember to extend the table
    above."""
    activating = {
        status
        for status in DataProductStatus
        if activation_intent_for(status) is ActivationIntent.ACTIVATE
    }
    assert activating == {DataProductStatus.ACTIVE}


def test_no_status_ever_maps_to_a_deactivate_intent() -> None:
    """Decision (module docstring, "whether v1 deactivates"): the intent vocabulary itself
    has no deactivate member, so this is actually checking the type, not just the
    function -- a future edit that added one would need to touch this test to compile."""
    assert {intent.value for intent in ActivationIntent} == {"activate", "none"}
