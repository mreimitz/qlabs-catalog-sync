"""Rule 6: one instant, one checksum, whatever offset and precision it arrived in."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from qlabs_catalog_sync_sdk.envelope import (
    TIMESTAMP_PRECISION_DIGITS,
    CanonicalizationError,
    canonical_json,
    compute_checksum,
    to_json_value,
)

UTC = UTC
CEST = timezone(timedelta(hours=2))
NEPAL = timezone(timedelta(hours=5, minutes=45))

INSTANT = datetime(2026, 8, 19, 15, 5, 30, 123000, tzinfo=UTC)


def test_the_precision_is_milliseconds() -> None:
    assert TIMESTAMP_PRECISION_DIGITS == 3


def test_the_same_instant_in_any_offset_hashes_the_same() -> None:
    spellings = [
        INSTANT,
        INSTANT.astimezone(CEST),
        INSTANT.astimezone(NEPAL),
        datetime(2026, 8, 19, 17, 5, 30, 123000, tzinfo=CEST),
    ]
    assert len({compute_checksum(value) for value in spellings}) == 1


def test_instants_render_as_utc_with_three_subsecond_digits() -> None:
    assert canonical_json(INSTANT.astimezone(CEST)) == '"2026-08-19T15:05:30.123Z"'


def test_a_whole_second_still_carries_its_millisecond_digits() -> None:
    whole = datetime(2026, 8, 19, 15, 5, 30, tzinfo=UTC)
    assert canonical_json(whole) == '"2026-08-19T15:05:30.000Z"'


def test_sub_millisecond_differences_are_deliberately_invisible() -> None:
    """Endpoints disagree below a millisecond; that noise must not look like an edit."""
    assert compute_checksum(INSTANT) == compute_checksum(INSTANT.replace(microsecond=123999))


def test_a_whole_millisecond_of_difference_is_visible() -> None:
    assert compute_checksum(INSTANT) != compute_checksum(INSTANT.replace(microsecond=124000))


def test_sub_second_digits_truncate_and_never_round_up() -> None:
    almost = datetime(2026, 8, 19, 15, 5, 30, 999999, tzinfo=UTC)
    assert canonical_json(almost) == '"2026-08-19T15:05:30.999Z"'


def test_rfc3339_strings_normalize_the_same_way_as_typed_datetimes() -> None:
    """Most timestamps reach the engine as JSON strings, so the rule must reach them."""
    spellings = [
        "2026-08-19T15:05:30.123Z",
        "2026-08-19T17:05:30.123+02:00",
        "2026-08-19t15:05:30.123456Z",
        "2026-08-19t15:05:30.123456z",
        "2026-08-19 17:05:30.123+0200",
        "2026-08-19T15:05:30.123000+00:00",
    ]
    digests = {compute_checksum(value) for value in spellings}
    assert digests == {compute_checksum(INSTANT)}


def test_a_string_without_an_offset_is_left_alone() -> None:
    """Nothing to normalize, and assuming a zone would invent information."""
    assert canonical_json("2026-08-19T15:05:30.123") == '"2026-08-19T15:05:30.123"'
    assert compute_checksum("2026-08-19T15:05:30.123") != compute_checksum(INSTANT)


def test_prose_that_merely_contains_a_timestamp_is_left_alone() -> None:
    prose = "Loaded at 2026-08-19T17:05:30+02:00 by the nightly job."
    assert canonical_json(prose) == f'"{prose}"'


def test_a_plain_date_stays_a_plain_date() -> None:
    assert canonical_json(date(2026, 8, 19)) == '"2026-08-19"'
    assert compute_checksum(date(2026, 8, 19)) == compute_checksum("2026-08-19")


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(CanonicalizationError):
        compute_checksum(datetime(2026, 8, 19, 15, 5, 30))


def test_stored_values_carry_the_canonical_instant() -> None:
    """So an envelope rebuilt from its own stored value still hashes the same."""
    assert to_json_value({"updated": INSTANT.astimezone(CEST)}) == {
        "updated": "2026-08-19T15:05:30.123Z"
    }
    assert compute_checksum(to_json_value(INSTANT)) == compute_checksum(INSTANT)
