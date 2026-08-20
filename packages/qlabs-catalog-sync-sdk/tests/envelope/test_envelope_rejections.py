"""What canonicalization refuses, so a silent wrong answer is never an option."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from qlabs_catalog_sync_sdk.envelope import CanonicalizationError, compute_checksum


def test_a_naive_datetime_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="naive"):
        compute_checksum(datetime(2026, 8, 19, 15, 5, 30))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    with pytest.raises(CanonicalizationError, match="JSON representation"):
        compute_checksum(value)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_decimals_are_rejected(value: Decimal) -> None:
    with pytest.raises(CanonicalizationError, match="JSON representation"):
        compute_checksum(value)


@pytest.mark.parametrize("value", [b"bytes", bytearray(b"bytes"), memoryview(b"bytes")])
def test_binary_values_are_rejected(value: object) -> None:
    with pytest.raises(CanonicalizationError, match="binary"):
        compute_checksum(value)


def test_a_non_string_object_key_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="keys must be strings"):
        compute_checksum({1: "one"})


def test_keys_that_collide_after_normalization_are_rejected_not_merged() -> None:
    """Silently dropping one of them would lose a field."""
    with pytest.raises(CanonicalizationError, match="duplicate object key"):
        compute_checksum({"Caf\u00e9": 1, "Cafe\u0301": 2})


def test_an_unsupported_type_is_rejected_by_name() -> None:
    class Opaque:
        pass

    with pytest.raises(CanonicalizationError, match="Opaque"):
        compute_checksum(Opaque())


def test_a_generator_is_rejected_because_it_is_not_reproducible() -> None:
    with pytest.raises(CanonicalizationError):
        compute_checksum(item for item in ("a", "b"))


def test_a_cycle_is_reported_instead_of_blowing_the_stack() -> None:
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    with pytest.raises(CanonicalizationError, match="cycle"):
        compute_checksum(cyclic)


def test_error_is_a_valueerror_so_callers_need_not_know_this_module() -> None:
    assert issubclass(CanonicalizationError, ValueError)
