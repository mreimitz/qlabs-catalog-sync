"""``endpoints``: round trip, defaults, and the duplicate-name constraint."""

from __future__ import annotations

import pytest
from configstore_helpers import NOW, make_endpoint
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qlabs_catalog_sync.configstore.models import EndpointRow
from qlabs_catalog_sync.configstore.types import EndpointRole


def test_endpoint_round_trips(session: Session) -> None:
    row = make_endpoint(
        "databricks_prod",
        connector="databricks",
        role=EndpointRole.SOURCE,
        secret_ref="env:DATABRICKS_PROD",
        settings={"host": "acme.cloud.databricks.com", "warehouse_id": "abc123"},
        enabled=True,
    )
    session.add(row)
    session.commit()
    session.expunge_all()

    found = session.get(EndpointRow, "databricks_prod")
    assert found is not None
    assert found.connector == "databricks"
    assert found.role == EndpointRole.SOURCE
    assert isinstance(found.role, EndpointRole)  # round-trips as the enum, not a bare str
    assert found.secret_ref == "env:DATABRICKS_PROD"
    assert found.settings == {"host": "acme.cloud.databricks.com", "warehouse_id": "abc123"}
    assert found.enabled is True
    assert found.created_at == NOW
    assert found.created_at.tzinfo is not None
    assert found.updated_at == NOW


def test_endpoint_settings_and_enabled_default(session: Session) -> None:
    row = EndpointRow(
        name="qlik_acme",
        connector="qlik",
        role=EndpointRole.TARGET,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(row)
    session.commit()
    session.expunge_all()

    found = session.get(EndpointRow, "qlik_acme")
    assert found is not None
    assert found.settings == {}
    assert found.enabled is False  # C6: registering is not the same as enabling
    assert found.secret_ref is None


def test_duplicate_endpoint_name_is_rejected(session: Session) -> None:
    session.add(make_endpoint("qlik_acme"))
    session.commit()

    session.add(make_endpoint("qlik_acme", connector="qlik"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_two_endpoints_with_different_names_coexist(session: Session) -> None:
    session.add(make_endpoint("qlik_acme", role=EndpointRole.TARGET))
    session.add(make_endpoint("databricks_prod", connector="databricks", role=EndpointRole.SOURCE))
    session.commit()

    names = set(session.execute(select(EndpointRow.name)).scalars().all())
    assert names == {"qlik_acme", "databricks_prod"}
