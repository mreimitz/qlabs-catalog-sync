"""Capability-honesty proofs the task brief names explicitly, pinned individually rather
than left as an implicit consequence of the base suite's generic loops.

``qlabs_catalog_sync_sdk.conformance.suite``'s
``test_writing_a_ro_or_na_field_raises_capability_error`` and
``test_unsupported_entities_refuse_writes_with_capability_error`` already cover this
generically (``TestQlikConformance`` in ``test_qlik_conformance_suite.py`` runs both).
What is added here is the same guarantee named field-by-field and action-by-action, so a
future regression on one specific path (say, ``DATASET`` create, or the D4 delete guard)
fails with an unambiguous, individually-named test rather than surfacing only as one
iteration of a loop — matching T4.6's ``test_write_refusal.py`` belt-and-suspenders shape
for the Databricks connector.

Every test here uses :func:`~.conftest.build_connector`, **not**
:func:`~.conftest.setup_connector` — see the latter's own docstring for why: this suite's
zero-HTTP-calls proof has to be reliable, and ``build_connector`` gives each test the
single respx router that makes ``assert_no_http_calls`` actually mean "no request was
sent" rather than "no request reached this particular router." That said, every refusal
below is also independently verifiable by reading the source: every guard clause in
``write.py``/``lifecycle.py`` this module exercises raises
:class:`~qlabs_catalog_sync_sdk.exceptions.CapabilityError` as a plain Python
early-return, before ``self._http.request(...)`` is ever called — so "zero calls" holds
on any transport, not merely "respx saw nothing" (see each test's own comment for the
exact guard).
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.conformance.harness import assert_no_http_calls
from qlabs_catalog_sync_sdk.conformance.samples import sample_entity, sample_value
from qlabs_catalog_sync_sdk.contract import Connector
from qlabs_catalog_sync_sdk.envelope import to_json_value
from qlabs_catalog_sync_sdk.exceptions import CapabilityError
from qlabs_catalog_sync_sdk.models import (
    EntityType,
    FieldChange,
    FieldDiff,
    FieldUpdateMode,
    IdentityRef,
)
from qlabs_connector_qlik.lifecycle import ALL_DESTRUCTIVE_ACTIONS, DestructiveAction

from .conftest import ENDPOINT, TENANT_ID, build_connector


def _ref(entity_type: EntityType, native_key: str = "conformance-synthetic-ref") -> IdentityRef:
    """A syntactically valid ref to an object that does not exist — safe for every test
    here because every guard below runs before any existence lookup (module docstring)."""
    return IdentityRef(
        endpoint=ENDPOINT, entity_type=entity_type, native_key=native_key, tenant_id=TENANT_ID
    )


def _diff(entity_type: EntityType, field: str, value: object) -> FieldDiff:
    return FieldDiff(
        entity_type=entity_type,
        changes=[FieldChange(field=field, mode=FieldUpdateMode.PATCH, value=to_json_value(value))],
        expected_revision=None,
    )


# --------------------------------------------------------------------------------------
# DATASET: every field is ro (decision D2) — the connector never creates or updates one.
# --------------------------------------------------------------------------------------


async def test_update_a_dataset_field_refuses_without_a_request(respx_mock: object) -> None:
    """``manifest.py``'s ``_dataset_capability()`` declares every ``DATASET`` field
    ``ro``. ``write.py``'s ``_ensure_entity_writable`` refuses *before* looking at any
    individual field, because the entity as a whole has no writable field at all — the
    connector never creates or updates a Qlik dataset (D2: datasets are resolved against
    the target space, never written)."""
    connector = await build_connector()
    try:
        diff = _diff(EntityType.DATASET, "name", "New Name")
        with pytest.raises(CapabilityError, match="read-only"), assert_no_http_calls():
            await connector.update(_ref(EntityType.DATASET), diff)
    finally:
        await connector.close()


async def test_create_a_dataset_refuses_without_a_request(respx_mock: object) -> None:
    """Symmetric with the update refusal above: ``create()`` on a ``DATASET`` is refused
    by the same "every field read-only" guard, for the same D2 reason."""
    connector = await build_connector()
    try:
        with pytest.raises(CapabilityError, match="read-only"), assert_no_http_calls():
            await connector.create(sample_entity(EntityType.DATASET))
    finally:
        await connector.close()


async def test_delete_a_dataset_refuses_without_a_request(respx_mock: object) -> None:
    connector = await build_connector()
    try:
        with pytest.raises(CapabilityError), assert_no_http_calls():
            await connector.delete(_ref(EntityType.DATASET))
    finally:
        await connector.close()


# --------------------------------------------------------------------------------------
# DATA_PRODUCT: the two fields that are individually ro/na, not entity-wide.
# --------------------------------------------------------------------------------------


async def test_update_glossary_term_refs_refuses_without_a_request(respx_mock: object) -> None:
    """``glossary_term_refs`` is declared ``na`` on ``DATA_PRODUCT`` — decision D5:
    Databricks (the only v1 source) has no glossary, so this connector has nothing to
    populate Qlik's ``glossaryIds`` from, even though the wire path exists."""
    connector = await build_connector()
    try:
        value = sample_value(EntityType.DATA_PRODUCT, "glossary_term_refs")
        diff = _diff(EntityType.DATA_PRODUCT, "glossary_term_refs", value)
        with pytest.raises(CapabilityError) as excinfo, assert_no_http_calls():
            await connector.update(_ref(EntityType.DATA_PRODUCT), diff)
        assert "na" in str(excinfo.value) or "glossary" in str(excinfo.value).lower()
    finally:
        await connector.close()


async def test_update_placement_refuses_without_a_request(respx_mock: object) -> None:
    """``placement`` is declared ``ro`` on ``DATA_PRODUCT``: ``/spaceId`` is writable at
    create and via the separate ``actions/move`` lifecycle endpoint, but it is not one of
    the eight PATCH paths this connector's field-level diff writer can reach."""
    connector = await build_connector()
    try:
        diff = _diff(EntityType.DATA_PRODUCT, "placement", "some-other-space")
        with pytest.raises(CapabilityError), assert_no_http_calls():
            await connector.update(_ref(EntityType.DATA_PRODUCT), diff)
    finally:
        await connector.close()


# --------------------------------------------------------------------------------------
# GLOSSARY_TERM / CATEGORY: unsupported entities (decision D5) — refused entirely.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("entity_type", [EntityType.GLOSSARY_TERM, EntityType.CATEGORY])
async def test_create_an_unsupported_entity_refuses_without_a_request(
    respx_mock: object, entity_type: EntityType
) -> None:
    connector = await build_connector()
    try:
        with pytest.raises(CapabilityError, match="does not support"), assert_no_http_calls():
            await connector.create(sample_entity(entity_type))
    finally:
        await connector.close()


# --------------------------------------------------------------------------------------
# D4: delete() on a default-constructed connector refuses and issues zero requests.
# --------------------------------------------------------------------------------------


async def test_delete_refuses_by_default_with_zero_requests(respx_mock: object) -> None:
    """The specific guarantee decision D4 makes: v1 never deletes in Qlik. A connector
    built the way ``Connector.__init__`` leaves it — ``enabled_destructive_actions``
    empty — refuses ``delete()`` on an otherwise perfectly valid ``DATA_PRODUCT`` ref,
    and ``lifecycle.py``'s ``_ensure_enabled`` is the *first* line of every one of
    ``LifecycleActions``'s four methods (module docstring: "checks enabled_actions before
    issuing any HTTP request"), so the refusal happens before any request is attempted.
    """
    connector = await build_connector()  # enabled_destructive_actions defaults to frozenset()
    try:
        with pytest.raises(CapabilityError, match="delete") as excinfo, assert_no_http_calls():
            await connector.delete(_ref(EntityType.DATA_PRODUCT))
        assert "off by default" in str(excinfo.value)
    finally:
        await connector.close()


@pytest.mark.parametrize(
    "action",
    [DestructiveAction.ACTIVATE, DestructiveAction.DEACTIVATE, DestructiveAction.MOVE],
)
async def test_lifecycle_action_refuses_by_default_with_zero_requests(
    respx_mock: object, action: DestructiveAction
) -> None:
    """The same D4/D7 opt-in guard, exercised for the other three lifecycle actions —
    reachable on a real ``Connector`` via ``connector.lifecycle``, not just through the
    ABC's ``delete()``. Enabling one action never enables the others (module docstring):
    a connector built with the default empty set refuses every one of them."""
    connector = await build_connector()
    try:
        assert connector.lifecycle is not None
        ref = _ref(EntityType.DATA_PRODUCT)
        with pytest.raises(CapabilityError, match=action.value), assert_no_http_calls():
            if action is DestructiveAction.ACTIVATE:
                await connector.lifecycle.activate(ref, name="x", managed_space_id="space-x")
            elif action is DestructiveAction.DEACTIVATE:
                await connector.lifecycle.deactivate(ref)
            else:
                await connector.lifecycle.move(ref, target_space_id="space-x")
    finally:
        await connector.close()


async def test_enabling_one_action_does_not_enable_delete(respx_mock: object) -> None:
    """Decision D4's most important negative case: naming ``ACTIVATE`` (the one action a
    future reconciliation task might legitimately opt into) must never smuggle ``DELETE``
    in as a side effect."""
    connector = await build_connector(
        enabled_destructive_actions=frozenset({DestructiveAction.ACTIVATE})
    )
    try:
        assert connector.lifecycle is not None
        with pytest.raises(CapabilityError, match="delete"), assert_no_http_calls():
            await connector.delete(_ref(EntityType.DATA_PRODUCT))
    finally:
        await connector.close()


async def test_every_destructive_action_enabled_is_still_just_a_named_opt_in(
    respx_mock: object,
) -> None:
    """Sanity check on :data:`ALL_DESTRUCTIVE_ACTIONS` itself — not a refusal test, but
    pins down that the convenience constant really does name all four and nothing more,
    so a future fifth action does not silently slip through unnamed."""
    assert frozenset(
        {
            DestructiveAction.ACTIVATE,
            DestructiveAction.DEACTIVATE,
            DestructiveAction.MOVE,
            DestructiveAction.DELETE,
        }
    ) == ALL_DESTRUCTIVE_ACTIONS
    connector: Connector = await build_connector(
        enabled_destructive_actions=ALL_DESTRUCTIVE_ACTIONS
    )
    await connector.close()
