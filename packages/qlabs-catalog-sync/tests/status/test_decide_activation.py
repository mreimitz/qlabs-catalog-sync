"""D7 fully gated: :func:`~qlabs_catalog_sync.sync.status.decide_activation`.

Pure and connector-free -- every combination of status / opt-in / managed-space is
exhaustively checked here without any I/O, before ``test_reconcile_activation.py`` proves
the same gates hold once a real (fake) connector is involved.
"""

from __future__ import annotations

from qlabs_catalog_sync.sync.loop import ACTIVATION_WITHHELD_REASON
from qlabs_catalog_sync.sync.status import (
    ACTIVATION_NOT_OPTED_IN_REASON,
    ACTIVATION_REQUIRES_MANAGED_SPACE_REASON,
    STATUS_NOT_ACTIVE_REASON,
    ActivationIntent,
    decide_activation,
)
from qlabs_catalog_sync_sdk.models import DataProductStatus


def test_active_opted_in_managed_is_requested() -> None:
    decision = decide_activation(
        DataProductStatus.ACTIVE, activation_opt_in=True, managed_space_id="managed-1"
    )
    assert decision.intent is ActivationIntent.ACTIVATE
    assert decision.requested is True
    assert decision.reason is None


def test_opt_in_off_is_never_requested_even_when_active_and_managed() -> None:
    """The DoD's hard half, at the decision layer: opt-in off blocks activation regardless
    of anything else being true."""
    decision = decide_activation(
        DataProductStatus.ACTIVE, activation_opt_in=False, managed_space_id="managed-1"
    )
    assert decision.requested is False
    assert decision.reason == ACTIVATION_NOT_OPTED_IN_REASON


def test_a_non_managed_space_is_never_requested_even_when_opted_in() -> None:
    decision = decide_activation(
        DataProductStatus.ACTIVE, activation_opt_in=True, managed_space_id=None
    )
    assert decision.requested is False
    assert decision.reason == ACTIVATION_REQUIRES_MANAGED_SPACE_REASON


def test_an_empty_managed_space_id_is_treated_the_same_as_none() -> None:
    """An explicit empty string is exactly as much "no managed space stated" as ``None`` --
    the caller must name a real id, not an empty one, to satisfy D7's managed-space gate."""
    decision = decide_activation(
        DataProductStatus.ACTIVE, activation_opt_in=True, managed_space_id=""
    )
    assert decision.requested is False
    assert decision.reason == ACTIVATION_REQUIRES_MANAGED_SPACE_REASON


def test_a_non_active_status_is_never_requested_regardless_of_the_other_two_gates() -> None:
    """This is also the deactivation test: an object whose status regressed away from
    ``active`` decides ``requested=False`` here -- no deactivate is ever proposed (module
    docstring, "whether v1 deactivates")."""
    statuses = (
        DataProductStatus.DRAFT,
        DataProductStatus.DEPRECATED,
        DataProductStatus.ARCHIVED,
        None,
    )
    for status in statuses:
        decision = decide_activation(status, activation_opt_in=True, managed_space_id="managed-1")
        assert decision.requested is False, status
        assert decision.intent is ActivationIntent.NONE, status
        assert decision.reason == STATUS_NOT_ACTIVE_REASON, status


def test_opt_in_reason_matches_loop_pys_field_level_withholding_reason() -> None:
    """"Consistent rather than duplicated" (T7.4's own instruction): this module's
    lifecycle-action gate reports the *same* reason code ``loop.py`` already uses for its
    field-level D7 withholding, so a report combining both never has to explain two
    spellings of the same decision. Asserted against the real, exported constant --
    ``sync.loop.ACTIVATION_WITHHELD_REASON`` -- not a copy-pasted string, so the two can
    never silently drift apart."""
    assert ACTIVATION_NOT_OPTED_IN_REASON == ACTIVATION_WITHHELD_REASON
