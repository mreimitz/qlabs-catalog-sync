"""A listing's publish state -> the neutral ``status`` enum (RS-05 sections 3.6/4.4:
"a sync that manages listings must model draft vs published state, not just field values")."""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.models import DataProductStatus
from qlabs_connector_snowflake.mapping import map_status

from .conftest import make_raw_listing, make_raw_schema


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("DRAFT", DataProductStatus.DRAFT),
        ("PUBLISHED", DataProductStatus.ACTIVE),
        ("LIVE", DataProductStatus.ACTIVE),
        ("UNPUBLISHED", DataProductStatus.ARCHIVED),
    ],
)
def test_each_known_state_maps_to_its_neutral_status(
    state: str, expected: DataProductStatus
) -> None:
    assert map_status(make_raw_listing(state=state)) == {"status": expected}


def test_state_matching_is_case_and_whitespace_insensitive() -> None:
    assert map_status({"state": " published "}) == {"status": DataProductStatus.ACTIVE}


def test_an_unrecognized_state_produces_an_explicit_none_rather_than_a_guess() -> None:
    """Inventing ``ACTIVE`` for an unknown state would publish a draft downstream."""
    assert map_status({"state": "PENDING_SOMETHING"}) == {"status": None}


def test_a_null_or_non_string_state_produces_an_explicit_none() -> None:
    assert map_status({"state": None}) == {"status": None}
    assert map_status({"state": 7}) == {"status": None}


def test_a_row_with_no_state_column_produces_no_fragment() -> None:
    raw = make_raw_listing()
    del raw["state"]

    assert map_status(raw) == {}
    assert map_status({}) == {}


def test_a_schema_row_has_no_lifecycle_and_so_gets_no_status() -> None:
    assert map_status(make_raw_schema()) == {}
