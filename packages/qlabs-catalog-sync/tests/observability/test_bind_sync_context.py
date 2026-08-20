"""``bind_sync_context``: binding, unbinding, nesting, and None-dropping semantics."""

from __future__ import annotations

import pytest
import structlog

from qlabs_catalog_sync.observability import bind_sync_context


def test_binds_the_four_named_fields() -> None:
    assert structlog.contextvars.get_contextvars() == {}

    with bind_sync_context(
        pair="db_to_qlik", endpoint="qlik_acme", entity_type="dataset", neutral_id="n-1"
    ):
        assert structlog.contextvars.get_contextvars() == {
            "pair": "db_to_qlik",
            "endpoint": "qlik_acme",
            "entity_type": "dataset",
            "neutral_id": "n-1",
        }

    assert structlog.contextvars.get_contextvars() == {}


def test_unset_fields_are_dropped_not_bound_as_none() -> None:
    with bind_sync_context(pair="db_to_qlik"):
        bound = structlog.contextvars.get_contextvars()
        assert bound == {"pair": "db_to_qlik"}
        assert "endpoint" not in bound
        assert "entity_type" not in bound
        assert "neutral_id" not in bound


def test_extra_kwargs_are_bound_too() -> None:
    with bind_sync_context(pair="db_to_qlik", run_id="run-42"):
        assert structlog.contextvars.get_contextvars() == {"pair": "db_to_qlik", "run_id": "run-42"}


def test_nested_binding_composes_and_unwinds_cleanly() -> None:
    with bind_sync_context(pair="db_to_qlik", endpoint="databricks_prod"):
        with bind_sync_context(entity_type="dataset", neutral_id="n-1"):
            assert structlog.contextvars.get_contextvars() == {
                "pair": "db_to_qlik",
                "endpoint": "databricks_prod",
                "entity_type": "dataset",
                "neutral_id": "n-1",
            }
        # Inner block's keys are gone; outer block's are untouched.
        assert structlog.contextvars.get_contextvars() == {
            "pair": "db_to_qlik",
            "endpoint": "databricks_prod",
        }
    assert structlog.contextvars.get_contextvars() == {}


def test_nested_binding_restores_a_shadowed_key_on_exit() -> None:
    with bind_sync_context(pair="db_to_qlik", endpoint="databricks_prod"):
        with bind_sync_context(endpoint="qlik_acme"):
            assert structlog.contextvars.get_contextvars()["endpoint"] == "qlik_acme"
        # The outer value for the shadowed key is restored, not left cleared.
        assert structlog.contextvars.get_contextvars()["endpoint"] == "databricks_prod"


def test_context_is_still_unbound_after_an_exception_inside_the_block() -> None:
    with pytest.raises(RuntimeError), bind_sync_context(pair="db_to_qlik"):
        raise RuntimeError("boom")

    assert structlog.contextvars.get_contextvars() == {}
