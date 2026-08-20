"""``owner`` -> ``Party``: email, service-principal application id, and group/other -- told
apart structurally, never guessed."""

from __future__ import annotations

from qlabs_catalog_sync_sdk.models import PartyRole
from qlabs_connector_databricks.mapping import map_owners, owner_party

from .conftest import make_raw_schema


def test_email_owner_lands_in_email_only() -> None:
    party = owner_party("sales-analytics@acme.com")

    assert party is not None
    assert party.email == "sales-analytics@acme.com"
    assert party.party_id is None
    assert party.display_name is None
    assert party.role is PartyRole.OWNER


def test_service_principal_application_id_lands_in_party_id_not_email() -> None:
    application_id = "e3b0c442-98fc-4e1c-8b1a-3f1b2c4d5e6f"

    party = owner_party(application_id)

    assert party is not None
    assert party.party_id == application_id
    assert party.email is None
    assert party.display_name is None
    assert party.role is PartyRole.OWNER


def test_uppercase_uuid_is_still_recognized_as_an_application_id() -> None:
    application_id = "E3B0C442-98FC-4E1C-8B1A-3F1B2C4D5E6F"

    party = owner_party(application_id)

    assert party is not None
    assert party.party_id == application_id
    assert party.email is None


def test_group_owner_lands_in_display_name() -> None:
    party = owner_party("data-engineers")

    assert party is not None
    assert party.display_name == "data-engineers"
    assert party.party_id is None
    assert party.email is None
    assert party.role is PartyRole.OWNER


def test_blank_or_missing_owner_is_none() -> None:
    assert owner_party("") is None
    assert owner_party("   ") is None
    assert owner_party(None) is None
    assert owner_party(42) is None  # a non-string is never a principal


def test_map_owners_absent_key_produces_no_fragment() -> None:
    raw = make_raw_schema()
    del raw["owner"]

    fields = map_owners(raw)

    assert fields == {}


def test_map_owners_blank_owner_produces_explicit_empty_list() -> None:
    raw = make_raw_schema(owner="")

    fields = map_owners(raw)

    assert fields == {"owners": []}


def test_map_owners_null_owner_produces_explicit_empty_list() -> None:
    raw = make_raw_schema(owner=None)

    fields = map_owners(raw)

    assert fields == {"owners": []}


def test_map_owners_email_owner_produces_single_element_list() -> None:
    raw = make_raw_schema(owner="sales-analytics@acme.com")

    fields = map_owners(raw)

    assert len(fields["owners"]) == 1
    assert fields["owners"][0].email == "sales-analytics@acme.com"


def test_map_owners_group_owner_produces_single_element_list() -> None:
    raw = make_raw_schema(owner="data-engineers")

    fields = map_owners(raw)

    assert len(fields["owners"]) == 1
    assert fields["owners"][0].display_name == "data-engineers"
    assert fields["owners"][0].email is None


def test_map_owners_missing_key_does_not_raise() -> None:
    assert map_owners({}) == {}
