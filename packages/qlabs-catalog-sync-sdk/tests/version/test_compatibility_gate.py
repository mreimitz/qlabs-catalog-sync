"""``check_contract_compatibility`` — the discovery-time gate the engine (T2.1) calls
once per connector class loaded from the ``qlabs_catalog_sync.connectors`` entry-point
group (RS-08 sections 3 and 7).
"""

from __future__ import annotations

import pytest

from qlabs_catalog_sync_sdk.version import (
    SDK_CONTRACT_VERSION,
    ContractVersionError,
    check_contract_compatibility,
)

from .conftest import NotAConnector, RealConnector, StaleConnector, UnstampedConnector


def test_accepts_a_real_connector_built_against_the_current_contract_version() -> None:
    # RealConnector never overrides the stamp — it is whatever Connector itself sets.
    assert RealConnector.sdk_contract_version == SDK_CONTRACT_VERSION

    assert check_contract_compatibility(RealConnector) is None


def test_rejects_a_mismatched_major_naming_the_connector_and_both_versions() -> None:
    with pytest.raises(ContractVersionError) as excinfo:
        check_contract_compatibility(StaleConnector)

    error = excinfo.value
    message = str(error)

    # Names which connector...
    assert StaleConnector.name in message
    # ...which version it was built against...
    assert str(StaleConnector.sdk_contract_version) in message
    # ...and which the SDK supports.
    assert str(SDK_CONTRACT_VERSION) in message

    assert error.connector.startswith(repr(StaleConnector.name))
    assert error.built_against == StaleConnector.sdk_contract_version
    assert error.sdk_supports == SDK_CONTRACT_VERSION


def test_rejects_a_class_that_is_not_a_connector_subclass() -> None:
    with pytest.raises(ContractVersionError) as excinfo:
        check_contract_compatibility(NotAConnector)

    error = excinfo.value
    assert "NotAConnector" in str(error)
    assert "Connector" in str(error)
    assert error.built_against is None
    assert error.sdk_supports == SDK_CONTRACT_VERSION


def test_rejects_a_non_class_object() -> None:
    """An entry point can hand back anything ``ep.load()`` resolves to — not only a
    class missing ``Connector`` in its MRO, but something that is not a class at all
    (a plain instance, in this case)."""
    with pytest.raises(ContractVersionError) as excinfo:
        check_contract_compatibility(object())

    assert excinfo.value.built_against is None


def test_rejects_a_connector_subclass_with_a_non_int_stamp() -> None:
    with pytest.raises(ContractVersionError) as excinfo:
        check_contract_compatibility(UnstampedConnector)

    error = excinfo.value
    assert UnstampedConnector.name in str(error)
    assert error.built_against is None
    assert error.sdk_supports == SDK_CONTRACT_VERSION


def test_sdk_contract_version_override_is_the_testing_seam_not_the_real_default() -> None:
    """The keyword-only override lets a test simulate "the SDK moved to major N+1"
    without monkeypatching the module constant; production callers never pass it."""
    with pytest.raises(ContractVersionError) as excinfo:
        check_contract_compatibility(RealConnector, sdk_contract_version=SDK_CONTRACT_VERSION + 1)

    error = excinfo.value
    assert error.built_against == SDK_CONTRACT_VERSION
    assert error.sdk_supports == SDK_CONTRACT_VERSION + 1


def test_contract_version_error_is_not_a_connector_error() -> None:
    """Deliberately not part of the exceptions.py hierarchy (see version.py's
    docstring): this is a class the SDK refuses to load, not a running connector's
    operation failing against one endpoint."""
    from qlabs_catalog_sync_sdk.exceptions import ConnectorError

    assert not issubclass(ContractVersionError, ConnectorError)
