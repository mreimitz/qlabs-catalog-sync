"""``QlikReferenceResolver.resolve_datasets`` — decision D2.

Covers: a tier-1 IdentityMap hit resolves with zero HTTP calls; a tier-1 miss falls
back to an exact name match scoped to the target space (and a same-named dataset in
another space does not leak in); a member with no match anywhere is reported unresolved
and absent from the output; two same-named datasets in the same space are ambiguous and
also dropped/reported; the name cache prevents a second HTTP call for a repeated name;
``DatasetResolution.subset_for`` stays a true subset of ``dataset_ids`` even after a
drop; and no code path ever issues a POST.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_catalog_sync_sdk.http import HttpEndpoint
from qlabs_connector_qlik.resolve import (
    DatasetIdentityLookup,
    DatasetMember,
    DatasetResolutionReason,
    QlikReferenceResolver,
)

from .conftest import ITEMS_URL, OTHER_SPACE_ID, SPACE_ID, TENANT_BASE_URL


def _resolver(
    http: HttpEndpoint, lookup: DatasetIdentityLookup, *, space_id: str = SPACE_ID
) -> QlikReferenceResolver:
    return QlikReferenceResolver(
        http, endpoint="qlik", space_id=space_id, dataset_identity_lookup=lookup
    )


async def test_tier_1_identity_hit_resolves_without_any_http_call(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    member = DatasetMember(neutral_id=uuid.uuid4(), name="orders")
    lookup = make_lookup({member.neutral_id: "ds-1"})
    resolver = _resolver(http, lookup)

    result = await resolver.resolve_datasets([member])

    assert result.resolved == {member.neutral_id: "ds-1"}
    assert result.unresolved == []
    assert result.dataset_ids == ["ds-1"]
    assert len(respx_mock.calls) == 0


async def test_tier_2_name_match_is_scoped_to_the_space(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # If the implementation forgot to scope by spaceId, hand back the *wrong*
        # space's dataset so the assertion below would fail loudly.
        space = request.url.params.get("spaceId")
        if space == SPACE_ID:
            data = [{"id": "item-a", "resourceId": "ds-a", "name": "orders"}]
        elif space == OTHER_SPACE_ID:
            data = [{"id": "item-b", "resourceId": "ds-b", "name": "orders"}]
        else:
            data = [
                {"id": "item-a", "resourceId": "ds-a", "name": "orders"},
                {"id": "item-b", "resourceId": "ds-b", "name": "orders"},
            ]
        return httpx.Response(200, json={"data": data, "links": {}})

    respx_mock.get(ITEMS_URL).mock(side_effect=handler)
    member = DatasetMember(neutral_id=uuid.uuid4(), name="orders")
    resolver = _resolver(http, make_lookup(None), space_id=SPACE_ID)

    result = await resolver.resolve_datasets([member])

    assert result.resolved == {member.neutral_id: "ds-a"}
    assert result.dataset_ids == ["ds-a"]
    assert "ds-b" not in result.dataset_ids


async def test_unresolved_member_is_reported_and_absent_from_output(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    respx_mock.get(ITEMS_URL).mock(
        return_value=httpx.Response(200, json={"data": [], "links": {}})
    )
    member = DatasetMember(neutral_id=uuid.uuid4(), name="ghost")
    resolver = _resolver(http, make_lookup(None))

    result = await resolver.resolve_datasets([member])

    assert result.resolved == {}
    assert result.dataset_ids == []
    assert len(result.unresolved) == 1
    unresolved = result.unresolved[0]
    assert unresolved.neutral_id == member.neutral_id
    assert unresolved.name == "ghost"
    assert unresolved.reason is DatasetResolutionReason.NOT_FOUND


async def test_ambiguous_name_match_is_dropped_and_reported_not_guessed(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    respx_mock.get(ITEMS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "item-a", "resourceId": "ds-a", "name": "orders"},
                    {"id": "item-b", "resourceId": "ds-b", "name": "orders"},
                ],
                "links": {},
            },
        )
    )
    member = DatasetMember(neutral_id=uuid.uuid4(), name="orders")
    resolver = _resolver(http, make_lookup(None))

    result = await resolver.resolve_datasets([member])

    assert result.resolved == {}
    assert len(result.unresolved) == 1
    unresolved = result.unresolved[0]
    assert unresolved.reason is DatasetResolutionReason.AMBIGUOUS
    assert unresolved.candidate_count == 2


async def test_name_cache_prevents_a_second_http_call(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    route = respx_mock.get(ITEMS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "item-a", "resourceId": "ds-a", "name": "orders"}], "links": {}},
        )
    )
    member_a = DatasetMember(neutral_id=uuid.uuid4(), name="orders")
    member_b = DatasetMember(neutral_id=uuid.uuid4(), name="orders")
    resolver = _resolver(http, make_lookup(None))

    result = await resolver.resolve_datasets([member_a, member_b])

    assert result.resolved == {member_a.neutral_id: "ds-a", member_b.neutral_id: "ds-a"}
    assert route.call_count == 1


async def test_clear_cache_forces_a_fresh_http_call(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    route = respx_mock.get(ITEMS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "item-a", "resourceId": "ds-a", "name": "orders"}], "links": {}},
        )
    )
    member = DatasetMember(neutral_id=uuid.uuid4(), name="orders")
    resolver = _resolver(http, make_lookup(None))

    await resolver.resolve_datasets([member])
    resolver.clear_cache()
    await resolver.resolve_datasets([member])

    assert route.call_count == 2


async def test_subset_for_stays_a_true_subset_after_a_drop(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    respx_mock.get(ITEMS_URL).mock(
        return_value=httpx.Response(200, json={"data": [], "links": {}})
    )
    resolved_member = DatasetMember(neutral_id=uuid.uuid4(), name="orders")
    dropped_member = DatasetMember(neutral_id=uuid.uuid4(), name="ghost")
    lookup = make_lookup({resolved_member.neutral_id: "ds-a"})
    resolver = _resolver(http, lookup)

    result = await resolver.resolve_datasets([resolved_member, dropped_member])

    desired_api_consumable = [resolved_member.neutral_id, dropped_member.neutral_id]
    subset = result.subset_for(desired_api_consumable)

    assert subset == ["ds-a"]
    assert set(subset) <= set(result.dataset_ids)


async def test_401_raises_auth_error(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    respx_mock.get(ITEMS_URL).mock(return_value=httpx.Response(401, json={"error": "no"}))
    member = DatasetMember(neutral_id=uuid.uuid4(), name="orders")
    resolver = _resolver(http, make_lookup(None))

    with pytest.raises(AuthError):
        await resolver.resolve_datasets([member])


async def test_429_is_retried_then_succeeds(
    respx_mock: object,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    route = respx_mock.get(ITEMS_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(
                200,
                json={
                    "data": [{"id": "item-a", "resourceId": "ds-a", "name": "orders"}],
                    "links": {},
                },
            ),
        ]
    )
    endpoint = HttpEndpoint(
        TENANT_BASE_URL,
        auth=("Bearer", "test-token"),
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.01,
    )
    try:
        member = DatasetMember(neutral_id=uuid.uuid4(), name="orders")
        resolver = _resolver(endpoint, make_lookup(None))

        result = await resolver.resolve_datasets([member])
    finally:
        await endpoint.aclose()

    assert result.dataset_ids == ["ds-a"]
    assert route.call_count == 2


async def test_no_code_path_ever_issues_a_post(
    respx_mock: object,
    http: HttpEndpoint,
    make_lookup: Callable[[dict[uuid.UUID, str] | None], DatasetIdentityLookup],
) -> None:
    respx_mock.get(ITEMS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "item-a", "resourceId": "ds-a", "name": "orders"}], "links": {}},
        )
    )
    members = [
        DatasetMember(neutral_id=uuid.uuid4(), name="orders"),
        DatasetMember(neutral_id=uuid.uuid4(), name="ghost"),
    ]
    resolver = _resolver(http, make_lookup(None))

    await resolver.resolve_datasets(members)

    assert all(call.request.method != "POST" for call in respx_mock.calls)
