"""FieldCapability: mode, writable_via, partial_update, and the classmethod builders."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qlabs_catalog_sync_sdk.manifest import FieldCapability, FieldCapabilityMode


def test_mode_is_required() -> None:
    with pytest.raises(ValidationError):
        FieldCapability()  # type: ignore[call-arg]


def test_rw_builder_defaults_to_partial_update() -> None:
    capability = FieldCapability.rw(writable_via="rest-patch")

    assert capability.mode is FieldCapabilityMode.RW
    assert capability.writable_via == "rest-patch"
    assert capability.partial_update is True
    assert capability.is_writable
    assert not capability.requires_full_replace


def test_rw_builder_can_declare_full_replace() -> None:
    capability = FieldCapability.rw(writable_via="rest-patch", partial_update=False)

    assert capability.requires_full_replace


def test_ro_and_na_are_never_writable_or_full_replace() -> None:
    assert not FieldCapability.ro().is_writable
    assert not FieldCapability.na().is_writable
    assert not FieldCapability.ro().requires_full_replace
    assert not FieldCapability.na().requires_full_replace


@pytest.mark.parametrize("mode", [FieldCapabilityMode.RO, FieldCapabilityMode.NA])
def test_writable_via_is_rejected_for_a_non_writable_field(mode: FieldCapabilityMode) -> None:
    with pytest.raises(ValidationError, match="writable_via"):
        FieldCapability(mode=mode, writable_via="rest-patch")


def test_round_trips_through_json() -> None:
    capability = FieldCapability.rw(writable_via="sql-ddl", partial_update=False)

    restored = FieldCapability.model_validate(capability.model_dump(mode="json"))

    assert restored == capability


def test_na_field_round_trips_too() -> None:
    capability = FieldCapability.na()

    restored = FieldCapability.model_validate(capability.model_dump(mode="json"))

    assert restored == capability
