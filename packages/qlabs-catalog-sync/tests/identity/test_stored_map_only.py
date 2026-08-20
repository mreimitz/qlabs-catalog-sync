"""After binding, the stored map is the only matcher -- and it is tenant-scoped.

RS-03 section 4: natural-key matching is a bootstrap mechanism, used once, before a
binding exists. These tests prove the two ways that can go wrong -- a rename that
re-triggers name matching, and a native key that leaks across tenants -- do not.
"""

from __future__ import annotations

import pytest
from helpers import SOURCE_TENANT, TARGET_ENDPOINT, dbx, qlik, run_bootstrap

from qlabs_catalog_sync.identity import IdentityResolver
from qlabs_catalog_sync_sdk.models import EntityType


async def test_renaming_the_source_still_resolves_and_never_re_matches_by_name(
    resolver: IdentityResolver,
) -> None:
    """The nastiest case: after the rename the source's name matches a *different* product.

    A name-matching resolver would happily rebind it. This one never looks at the name
    again, so the original binding stands and bootstrap produces no proposal at all.
    """
    source = dbx("sch-0001", "sales")
    candidates = [qlik("dp-sales", "sales"), qlik("dp-orders", "orders")]
    first = await run_bootstrap(resolver, [source], candidates)
    bound = await resolver.confirm(first.proposed[0].proposal_id)

    # The Databricks schema is renamed to "orders". Its native key (a stable id) is unchanged.
    renamed = dbx("sch-0001", "orders")
    assert renamed.identity == source.identity

    binding = await resolver.resolve(renamed.identity)
    assert binding is not None
    assert binding.neutral_id == bound.neutral_id

    second = await run_bootstrap(resolver, [renamed], candidates)

    assert second.proposed == ()
    assert second.ambiguous == ()
    assert second.unmatched == ()
    assert len(second.already_bound) == 1
    assert "Natural-key matching is a bootstrap mechanism only" in (
        second.already_bound[0].rationale
    )

    counterpart = await resolver.counterpart(
        bound.neutral_id, TARGET_ENDPOINT, EntityType.DATA_PRODUCT
    )
    assert counterpart is not None
    assert counterpart.identity.native_key == "dp-sales"  # not dp-orders
    assert len(await resolver.list_bindings(endpoint=TARGET_ENDPOINT)) == 1


async def test_renaming_the_target_still_resolves_and_never_re_matches_by_name(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-0001", "sales")
    first = await run_bootstrap(resolver, [source], [qlik("dp-sales", "sales")])
    bound = await resolver.confirm(first.proposed[0].proposal_id)

    # The Qlik product is renamed; its native key does not change.
    renamed_candidates = [qlik("dp-sales", "revenue"), qlik("dp-new", "sales")]
    second = await run_bootstrap(resolver, [source], renamed_candidates)

    assert len(second.already_bound) == 1
    assert second.proposed == ()
    counterpart = await resolver.counterpart(
        bound.neutral_id, TARGET_ENDPOINT, EntityType.DATA_PRODUCT
    )
    assert counterpart is not None
    assert counterpart.identity.native_key == "dp-sales"
    assert len(await resolver.list_bindings(endpoint=TARGET_ENDPOINT)) == 1


async def test_a_source_whose_native_key_changed_is_a_new_object_not_a_rebind(
    resolver: IdentityResolver,
) -> None:
    """A source that renames its *key* is unknown to the map -- and still only a proposal.

    The already-bound Qlik product is not offered as a candidate, because it belongs to
    the neutral id the old key still maps to.
    """
    old = dbx("sch-0001", "sales")
    candidate = qlik("dp-sales", "sales")
    first = await run_bootstrap(resolver, [old], [candidate])
    await resolver.confirm(first.proposed[0].proposal_id)

    new = dbx("sch-0002", "sales")
    second = await run_bootstrap(resolver, [new], [candidate])

    assert second.proposed == ()
    assert len(second.unmatched) == 1
    assert second.unmatched[0].excluded_bound_candidates == ("dp-sales",)
    assert len(await resolver.list_bindings(endpoint=TARGET_ENDPOINT)) == 1


async def test_a_native_key_from_another_tenant_does_not_resolve(
    resolver: IdentityResolver,
) -> None:
    """Native keys are only unique within a tenant, so every lookup carries the tenant."""
    tenant_a = dbx("main.sales", "sales", tenant_id=SOURCE_TENANT)
    tenant_b = dbx("main.sales", "sales", tenant_id="dbx-account-b")
    assert tenant_a.identity.native_key == tenant_b.identity.native_key

    await resolver.register_source(tenant_a.identity)

    assert await resolver.resolve(tenant_a.identity) is not None
    assert await resolver.resolve(tenant_b.identity) is None


async def test_binding_one_tenant_leaves_the_other_tenants_key_unbound(
    resolver: IdentityResolver,
) -> None:
    tenant_a = dbx("main.sales", "sales", tenant_id=SOURCE_TENANT)
    tenant_b = dbx("main.sales", "sales", tenant_id="dbx-account-b")
    candidate = qlik("dp-sales", "sales")

    first = await run_bootstrap(resolver, [tenant_a], [candidate])
    bound = await resolver.confirm(first.proposed[0].proposal_id)

    # The same native key in another tenant is still a stranger to the map.
    second = await run_bootstrap(resolver, [tenant_b], [candidate])

    assert second.already_bound == ()
    assert len(second.unmatched) == 1
    assert second.unmatched[0].excluded_bound_candidates == ("dp-sales",)

    binding_b = await resolver.resolve(tenant_b.identity)
    assert binding_b is None
    binding_a = await resolver.resolve(tenant_a.identity)
    assert binding_a is not None
    assert binding_a.neutral_id == bound.neutral_id


async def test_two_tenants_holding_the_same_native_key_bind_independently(
    resolver: IdentityResolver,
) -> None:
    tenant_a = dbx("main.sales", "sales", tenant_id=SOURCE_TENANT)
    tenant_b = dbx("main.sales", "sales", tenant_id="dbx-account-b")

    a = await resolver.register_source(tenant_a.identity)
    b = await resolver.register_source(tenant_b.identity)

    assert a.neutral_id != b.neutral_id
    assert a.identity.tenant_id == SOURCE_TENANT
    assert b.identity.tenant_id == "dbx-account-b"


async def test_resolve_of_an_unknown_key_is_none_not_an_error(
    resolver: IdentityResolver,
) -> None:
    assert await resolver.resolve(dbx("sch-nope", "nothing").identity) is None
    assert await resolver.resolve_neutral_id(dbx("sch-nope", "nothing").identity) is None


async def test_list_bindings_filters_by_endpoint_type_and_tenant(
    resolver: IdentityResolver,
) -> None:
    source = dbx("sch-0001", "sales")
    report = await run_bootstrap(resolver, [source], [qlik("dp-sales", "sales")])
    await resolver.confirm(report.proposed[0].proposal_id)

    assert len(await resolver.list_bindings()) == 2
    assert len(await resolver.list_bindings(endpoint=TARGET_ENDPOINT)) == 1
    assert len(await resolver.list_bindings(tenant_id=SOURCE_TENANT)) == 1
    assert len(await resolver.list_bindings(entity_type=EntityType.DATASET)) == 0
    assert len(await resolver.list_bindings(confirmed_only=True)) == 2


@pytest.mark.parametrize("entity_type", [EntityType.DATASET, EntityType.GLOSSARY_TERM])
async def test_matching_is_the_same_for_every_entity_type(
    resolver: IdentityResolver, entity_type: EntityType
) -> None:
    source = dbx("obj-1", "sales", entity_type=entity_type)
    report = await run_bootstrap(
        resolver, [source], [qlik("tgt-1", "sales", entity_type=entity_type)]
    )
    result = await resolver.confirm(report.proposed[0].proposal_id)

    assert result.neutral_id is not None
    counterpart = await resolver.counterpart(result.neutral_id, TARGET_ENDPOINT, entity_type)
    assert counterpart is not None
    assert counterpart.identity.entity_type is entity_type
