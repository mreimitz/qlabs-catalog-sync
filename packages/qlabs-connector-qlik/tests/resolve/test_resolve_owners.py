"""``QlikReferenceResolver.resolve_owners`` — decision D3.

Covers: an owner email resolves to a Qlik ``userId``; an owner with no email at all is
reported and dropped without any HTTP call; an unmatched email is reported and dropped;
two distinct users matching the same email are ambiguous and dropped; the same person
arriving twice under different roles yields exactly one ``keyContacts`` entry, keeping
the higher-priority role; the email cache prevents a second HTTP call; and a 401 raises
``AuthError``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_catalog_sync_sdk.models import Party, PartyRole
from qlabs_connector_qlik.resolve import (
    DatasetIdentityLookup,
    OwnerResolutionReason,
    QlikReferenceResolver,
)

from .conftest import USERS_URL


def _resolver(http: HttpEndpoint, lookup: DatasetIdentityLookup) -> QlikReferenceResolver:
    return QlikReferenceResolver(
        http, endpoint="qlik", space_id="space-123", dataset_identity_lookup=lookup
    )


async def test_email_resolves_to_a_qlik_user_id(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    respx_mock.get(USERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "user-1", "email": "alice@acme.example"}],
                "links": {},
            },
        )
    )
    owner = Party(email="alice@acme.example", role=PartyRole.OWNER)
    resolver = _resolver(http, make_lookup(None))

    result = await resolver.resolve_owners([owner])

    assert result.unmatched == []
    assert len(result.key_contacts) == 1
    assert result.key_contacts[0].user_id == "user-1"
    assert result.key_contacts[0].role == "owner"
    assert result.key_contacts[0].as_json() == {"userId": "user-1", "role": "owner"}


async def test_owner_with_no_email_is_reported_without_any_http_call(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    owner = Party(display_name="Data Team", role=PartyRole.STEWARD)
    resolver = _resolver(http, make_lookup(None))

    result = await resolver.resolve_owners([owner])

    assert result.key_contacts == []
    assert len(result.unmatched) == 1
    assert result.unmatched[0].reason is OwnerResolutionReason.NO_EMAIL
    assert result.unmatched[0].party is owner
    assert len(respx_mock.calls) == 0


async def test_unmatched_email_is_reported_and_dropped(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    respx_mock.get(USERS_URL).mock(return_value=httpx.Response(200, json={"data": [], "links": {}}))
    owner = Party(email="nobody@acme.example", role=PartyRole.OWNER)
    resolver = _resolver(http, make_lookup(None))

    result = await resolver.resolve_owners([owner])

    assert result.key_contacts == []
    assert len(result.unmatched) == 1
    assert result.unmatched[0].reason is OwnerResolutionReason.NOT_FOUND


async def test_two_distinct_users_for_one_email_is_ambiguous_not_guessed(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    respx_mock.get(USERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "user-1", "email": "shared@acme.example"},
                    {"id": "user-2", "email": "shared@acme.example"},
                ],
                "links": {},
            },
        )
    )
    owner = Party(email="shared@acme.example", role=PartyRole.OWNER)
    resolver = _resolver(http, make_lookup(None))

    result = await resolver.resolve_owners([owner])

    assert result.key_contacts == []
    assert result.unmatched[0].reason is OwnerResolutionReason.AMBIGUOUS


async def test_same_user_twice_yields_one_key_contact_keeping_higher_priority_role(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    route = respx_mock.get(USERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "user-1", "email": "alice@acme.example"}], "links": {}},
        )
    )
    # STEWARD arrives first, OWNER second — the dedup must keep OWNER (higher
    # priority), not "whichever came first".
    owners = [
        Party(email="alice@acme.example", role=PartyRole.STEWARD),
        Party(email="alice@acme.example", role=PartyRole.OWNER),
    ]
    resolver = _resolver(http, make_lookup(None))

    result = await resolver.resolve_owners(owners)

    assert len(result.key_contacts) == 1
    assert result.key_contacts[0].user_id == "user-1"
    assert result.key_contacts[0].role == "owner"
    # One HTTP call for the (identical, cached) email, not two.
    assert route.call_count == 1


async def test_email_cache_prevents_a_second_http_call_across_owners(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    route = respx_mock.get(USERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "user-1", "email": "alice@acme.example"}], "links": {}},
        )
    )
    resolver = _resolver(http, make_lookup(None))

    await resolver.resolve_owners([Party(email="alice@acme.example", role=PartyRole.OWNER)])
    await resolver.resolve_owners([Party(email="alice@acme.example", role=PartyRole.STEWARD)])

    assert route.call_count == 1


async def test_401_raises_auth_error(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    respx_mock.get(USERS_URL).mock(return_value=httpx.Response(401, json={"error": "no"}))
    owner = Party(email="alice@acme.example", role=PartyRole.OWNER)
    resolver = _resolver(http, make_lookup(None))

    with pytest.raises(AuthError):
        await resolver.resolve_owners([owner])


async def test_no_code_path_ever_issues_a_post(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    respx_mock.get(USERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "user-1", "email": "alice@acme.example"}], "links": {}},
        )
    )
    owners = [
        Party(email="alice@acme.example", role=PartyRole.OWNER),
        Party(email="nobody@acme.example", role=PartyRole.CONTACT),
    ]
    resolver = _resolver(http, make_lookup(None))

    await resolver.resolve_owners(owners)

    assert all(call.request.method != "POST" for call in respx_mock.calls)
