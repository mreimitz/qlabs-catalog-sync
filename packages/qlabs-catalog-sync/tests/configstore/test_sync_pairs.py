"""``sync_pairs``: round trip (entity types, manual-edit policy), FKs to endpoints, checks."""

from __future__ import annotations

import uuid

import pytest
from configstore_helpers import NOW, make_endpoint, make_sync_pair, sample_manual_edit_policy
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qlabs_catalog_sync.config import ManualEditMode, ManualEditPolicy
from qlabs_catalog_sync.configstore.models import EndpointRow, SyncPairRow
from qlabs_catalog_sync.configstore.types import EndpointRole
from qlabs_catalog_sync_sdk.models import EntityType


def _seed_endpoints(session: Session) -> None:
    session.add(make_endpoint("databricks_prod", connector="databricks", role=EndpointRole.SOURCE))
    session.add(make_endpoint("qlik_acme", connector="qlik", role=EndpointRole.TARGET))
    session.commit()


def test_sync_pair_round_trips_entity_types_and_manual_edit_policy(session: Session) -> None:
    _seed_endpoints(session)
    policy = sample_manual_edit_policy()
    row = make_sync_pair(
        entity_types=[EntityType.DATA_PRODUCT, EntityType.DATASET],
        cadence_seconds=1800,
        jitter_seconds=45.5,
        manual_edit_policy=policy,
        activation_opt_in=True,
        enabled=True,
    )
    session.add(row)
    session.commit()
    pair_id = row.id
    session.expunge_all()

    found = session.get(SyncPairRow, pair_id)
    assert found is not None
    assert found.source == "databricks_prod"
    assert found.target == "qlik_acme"
    assert found.entity_types == [EntityType.DATA_PRODUCT, EntityType.DATASET]
    assert all(isinstance(entity_type, EntityType) for entity_type in found.entity_types)
    assert found.cadence_seconds == 1800
    assert found.jitter_seconds == 45.5
    assert found.activation_opt_in is True
    assert found.enabled is True

    assert isinstance(found.manual_edit_policy, ManualEditPolicy)
    assert found.manual_edit_policy == policy
    assert found.manual_edit_policy.per_entity[EntityType.GLOSSARY_TERM] == (
        ManualEditMode.PRESERVE_LOCAL
    )
    assert found.manual_edit_policy.mode_for(EntityType.GLOSSARY_TERM) == (
        ManualEditMode.PRESERVE_LOCAL
    )
    assert found.manual_edit_policy.mode_for(EntityType.DATA_PRODUCT) == ManualEditMode.SOURCE_WINS


def test_sync_pair_defaults(session: Session) -> None:
    _seed_endpoints(session)
    row = SyncPairRow(
        name="minimal-pair",
        source="databricks_prod",
        target="qlik_acme",
        target_space="personal",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(row)
    session.commit()
    pair_id = row.id
    session.expunge_all()

    found = session.get(SyncPairRow, pair_id)
    assert found is not None
    assert found.entity_types == []
    assert found.cadence_seconds == 900
    assert found.jitter_seconds is None
    assert found.manual_edit_policy == ManualEditPolicy()
    assert found.activation_opt_in is False
    assert found.enabled is False  # matches decision D7's "off by default"


def test_duplicate_sync_pair_name_is_rejected(session: Session) -> None:
    _seed_endpoints(session)
    session.add(make_sync_pair("dup-pair"))
    session.commit()

    session.add(make_sync_pair("dup-pair"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_non_positive_cadence_is_rejected(session: Session) -> None:
    _seed_endpoints(session)
    session.add(make_sync_pair(cadence_seconds=0))
    with pytest.raises(IntegrityError):
        session.commit()


def test_pair_referencing_nonexistent_source_endpoint_is_rejected(session: Session) -> None:
    session.add(make_endpoint("qlik_acme", connector="qlik", role=EndpointRole.TARGET))
    session.commit()

    session.add(make_sync_pair(source="does_not_exist"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_pair_referencing_nonexistent_target_endpoint_is_rejected(session: Session) -> None:
    session.add(make_endpoint("databricks_prod", connector="databricks", role=EndpointRole.SOURCE))
    session.commit()

    session.add(make_sync_pair(target="does_not_exist"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_an_endpoint_in_use_by_a_pair_is_rejected(session: Session) -> None:
    """Endpoints are ``ondelete="RESTRICT"`` -- a pair keeps an in-use endpoint alive."""
    _seed_endpoints(session)
    session.add(make_sync_pair())
    session.commit()

    target_endpoint = session.get(EndpointRow, "qlik_acme")
    assert target_endpoint is not None
    session.delete(target_endpoint)
    with pytest.raises(IntegrityError):
        session.commit()


def test_two_pairs_can_share_the_same_source_and_target(session: Session) -> None:
    """Nothing in the schema limits an endpoint to one pair -- multiple pairs may reuse it."""
    _seed_endpoints(session)
    session.add(make_sync_pair("pair-a"))
    session.add(make_sync_pair("pair-b"))
    session.commit()

    ids = {row.id for row in session.query(SyncPairRow).all()}
    assert len(ids) == 2
    assert all(isinstance(pair_id, uuid.UUID) for pair_id in ids)
