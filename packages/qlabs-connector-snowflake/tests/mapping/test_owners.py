"""Owning **role** -> ``Party``: a display name, never an email.

This is the deliberate divergence from the Databricks connector, where an owner genuinely
is a user email / group / service-principal id and is classified into ``Party.email`` or
``Party.party_id``. A Snowflake owner is a role (RS-05 sections 1.3/4.2), so fabricating an
address would let a role silently correlate against a real person in the engine's
email-keyed owner correlation.
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.models import PartyRole
from qlabs_connector_snowflake.mapping import map_owners, owner_party

from .conftest import make_raw_listing, make_raw_schema, make_raw_table


def test_a_role_lands_in_display_name_and_never_in_email() -> None:
    party = owner_party("SALES_ENGINEER")

    assert party is not None
    assert party.display_name == "SALES_ENGINEER"
    assert party.email is None
    assert party.party_id is None
    assert party.role is PartyRole.OWNER


def test_an_email_shaped_role_name_is_still_not_written_to_email() -> None:
    """Even a role that happens to look like an address is a role. Writing it to
    ``Party.email`` would make the engine's owner correlation treat it as a person."""
    party = owner_party("SVC_SYNC@ACME")

    assert party is not None
    assert party.display_name == "SVC_SYNC@ACME"
    assert party.email is None


def test_surrounding_whitespace_is_trimmed() -> None:
    party = owner_party("  ACCOUNTADMIN  ")

    assert party is not None
    assert party.display_name == "ACCOUNTADMIN"


@pytest.mark.parametrize("value", ["", "   ", None, 42, ["SYSADMIN"]])
def test_a_value_carrying_no_role_is_none(value: object) -> None:
    assert owner_party(value) is None


def test_table_owner_column_is_used_for_a_table_row() -> None:
    fields = map_owners(make_raw_table(TABLE_OWNER="SALES_ENGINEER"))

    assert len(fields["owners"]) == 1
    assert fields["owners"][0].display_name == "SALES_ENGINEER"


def test_schema_owner_column_is_used_for_a_schema_row() -> None:
    """``INFORMATION_SCHEMA.SCHEMATA`` names the column ``SCHEMA_OWNER``, not
    ``TABLE_OWNER`` -- one function serves both via the fallback chain."""
    fields = map_owners(make_raw_schema(SCHEMA_OWNER="SYSADMIN"))

    assert len(fields["owners"]) == 1
    assert fields["owners"][0].display_name == "SYSADMIN"


def test_listing_owner_column_is_used_when_asked_for_explicitly() -> None:
    fields = map_owners(make_raw_listing(), source_keys=("owner",))

    assert len(fields["owners"]) == 1
    assert fields["owners"][0].display_name == "SALES_PROVIDER"


def test_the_first_present_key_in_the_chain_wins() -> None:
    fields = map_owners({"TABLE_OWNER": "FIRST", "SCHEMA_OWNER": "SECOND"})

    assert [party.display_name for party in fields["owners"]] == ["FIRST"]


def test_absent_owner_column_produces_no_fragment() -> None:
    raw = make_raw_table()
    del raw["TABLE_OWNER"]

    assert map_owners(raw) == {}


def test_null_owner_produces_an_explicit_empty_list() -> None:
    assert map_owners(make_raw_table(TABLE_OWNER=None)) == {"owners": []}


def test_blank_owner_produces_an_explicit_empty_list() -> None:
    assert map_owners(make_raw_table(TABLE_OWNER="")) == {"owners": []}


def test_snowflake_reports_at_most_one_owning_role() -> None:
    fields = map_owners(make_raw_table())

    assert len(fields["owners"]) == 1


def test_missing_keys_never_raise() -> None:
    assert map_owners({}) == {}
