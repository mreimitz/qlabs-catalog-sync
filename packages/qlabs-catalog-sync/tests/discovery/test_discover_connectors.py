"""discover_connectors: enumeration, the SDK version gate, the name/entry-point check,
the collision policy, and how a broken entry point is handled.

Every entry point here is a real ``importlib.metadata.EntryPoint`` (see ``helpers.py``),
loaded from real modules beside this file (``fixtures.py``, ``broken_fixture.py``) via
the real ``EntryPoint.load()`` — nothing here monkeypatches ``discovery`` itself, only
the *enumeration* ``discover_connectors`` is handed, exactly as the module's own
docstring says its testing seam is meant to be used.
"""

from __future__ import annotations

import pytest
from discovery_entry_points import entry_point
from fixtures import AnotherGoodConnector, GoodConnector

from qlabs_catalog_sync.discovery import (
    ConnectorBrokenError,
    ConnectorCollisionError,
    ConnectorNotRegisteredError,
    discover_connectors,
)

# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


def test_a_real_connector_subclass_is_discovered_and_registered_by_entry_point_name() -> None:
    registry = discover_connectors([entry_point("good", "fixtures:GoodConnector")])

    assert registry.names() == ("good",)
    assert registry.get_connector("good") is GoodConnector
    assert registry.broken() == {}
    assert "good" in registry
    assert len(registry) == 1


def test_multiple_independent_connectors_are_all_registered() -> None:
    registry = discover_connectors(
        [
            entry_point("good", "fixtures:GoodConnector"),
            entry_point("another_good", "fixtures:AnotherGoodConnector"),
        ]
    )

    assert set(registry.names()) == {"good", "another_good"}
    assert registry.get_connector("good") is GoodConnector
    assert registry.get_connector("another_good") is AnotherGoodConnector


def test_no_installed_entry_points_gives_an_empty_but_usable_registry() -> None:
    registry = discover_connectors([])

    assert registry.names() == ()
    assert registry.broken() == {}
    assert len(registry) == 0


# --------------------------------------------------------------------------------------
# The SDK contract-version gate
# --------------------------------------------------------------------------------------


def test_a_class_built_against_a_mismatched_contract_major_is_rejected_and_named() -> None:
    registry = discover_connectors([entry_point("wrong_major", "fixtures:WrongMajorConnector")])

    assert registry.names() == ()
    broken = registry.broken()["wrong_major"]
    assert broken.stage == "contract"
    assert "wrong_major" in broken.reason
    assert "WrongMajorConnector" in broken.reason

    with pytest.raises(ConnectorBrokenError, match="wrong_major"):
        registry.get_connector("wrong_major")


def test_a_non_connector_object_is_rejected() -> None:
    registry = discover_connectors([entry_point("not_a_connector", "fixtures:NotAConnector")])

    assert registry.names() == ()
    broken = registry.broken()["not_a_connector"]
    assert broken.stage == "contract"
    assert "Connector subclass" in broken.reason


# --------------------------------------------------------------------------------------
# The name / entry-point mismatch check
# --------------------------------------------------------------------------------------


def test_declared_name_not_matching_the_entry_point_name_is_treated_as_broken() -> None:
    """A mismatch means config, logs and the IdentityMap would disagree about what to
    call the endpoint — not registered under either name, and reported clearly."""
    registry = discover_connectors(
        [entry_point("declared_differently", "fixtures:MisnamedConnector")]
    )

    assert registry.names() == ()
    assert "actual-declared-name" not in registry  # never registered under its own name either
    broken = registry.broken()["declared_differently"]
    assert broken.stage == "name_mismatch"
    assert "declared_differently" in broken.reason
    assert "actual-declared-name" in broken.reason


# --------------------------------------------------------------------------------------
# Collision policy: fatal, names both distributions
# --------------------------------------------------------------------------------------


def test_two_distributions_claiming_the_same_name_fails_with_both_named() -> None:
    eps = [
        entry_point("qlik", "fixtures:GoodConnector", distribution_name="qlabs-connector-qlik"),
        entry_point(
            "qlik", "fixtures:AnotherGoodConnector", distribution_name="qlabs-connector-databricks"
        ),
    ]

    with pytest.raises(ConnectorCollisionError) as excinfo:
        discover_connectors(eps)

    error = excinfo.value
    assert error.collisions["qlik"] == ("qlabs-connector-qlik", "qlabs-connector-databricks")
    message = str(error)
    assert "qlik" in message
    assert "qlabs-connector-qlik" in message
    assert "qlabs-connector-databricks" in message


def test_collision_aborts_discovery_entirely_even_for_unrelated_names() -> None:
    """A collision on one name means nothing discovered is trustworthy yet — a good,
    unrelated connector must not be quietly registered alongside the failure."""
    eps = [
        entry_point("good", "fixtures:GoodConnector"),
        entry_point(
            "disputed", "fixtures:GoodConnector", distribution_name="qlabs-connector-qlik"
        ),
        entry_point(
            "disputed",
            "fixtures:AnotherGoodConnector",
            distribution_name="qlabs-connector-databricks",
        ),
    ]

    with pytest.raises(ConnectorCollisionError):
        discover_connectors(eps)


def test_same_name_same_distribution_is_not_a_collision() -> None:
    """A duplicate entry-points.txt line from one package is not two packages fighting."""
    eps = [
        entry_point("good", "fixtures:GoodConnector", distribution_name="qlabs-catalog-sync"),
        entry_point("good", "fixtures:GoodConnector", distribution_name="qlabs-catalog-sync"),
    ]

    registry = discover_connectors(eps)

    assert registry.names() == ("good",)


# --------------------------------------------------------------------------------------
# A broken ep.load() does not take discovery down
# --------------------------------------------------------------------------------------


def test_a_connector_whose_load_raises_is_reported_not_fatal() -> None:
    registry = discover_connectors(
        [
            entry_point("good", "fixtures:GoodConnector"),
            entry_point("broken", "broken_fixture:Whatever"),
        ]
    )

    # The broken entry did not take the working one down with it.
    assert registry.names() == ("good",)
    broken = registry.broken()["broken"]
    assert broken.stage == "load"
    assert "ModuleNotFoundError" in broken.reason or "not_a_real_vendor_sdk" in broken.reason

    with pytest.raises(ConnectorBrokenError, match="broken"):
        registry.get_connector("broken")


# --------------------------------------------------------------------------------------
# Lookup errors
# --------------------------------------------------------------------------------------


def test_looking_up_an_unregistered_name_errors_clearly() -> None:
    registry = discover_connectors([entry_point("good", "fixtures:GoodConnector")])

    with pytest.raises(ConnectorNotRegisteredError) as excinfo:
        registry.get_connector("nonexistent")

    message = str(excinfo.value)
    assert "nonexistent" in message
    assert "good" in message  # names what *is* installed, to help fix the config


def test_broken_and_not_registered_are_distinguishable_lookup_errors() -> None:
    """The whole point of recording broken entries: 'installed but broken' must not read
    the same as 'not installed at all'."""
    registry = discover_connectors([entry_point("broken", "broken_fixture:Whatever")])

    with pytest.raises(ConnectorBrokenError):
        registry.get_connector("broken")
    with pytest.raises(ConnectorNotRegisteredError):
        registry.get_connector("never_heard_of_it")


# --------------------------------------------------------------------------------------
# sdk_contract_version is a testing seam, forwarded to the SDK gate unchanged
# --------------------------------------------------------------------------------------


def test_sdk_contract_version_override_can_reject_an_otherwise_good_connector() -> None:
    """A connector built against the current contract major is rejected if the engine
    is (hypothetically) pinned to a different one — proves the parameter is really
    forwarded to qlabs_catalog_sync_sdk.version.check_contract_compatibility, not
    ignored."""
    registry = discover_connectors(
        [entry_point("good", "fixtures:GoodConnector")], sdk_contract_version=999
    )

    assert registry.names() == ()
    broken = registry.broken()["good"]
    assert broken.stage == "contract"


# --------------------------------------------------------------------------------------
# Deterministic ordering
# --------------------------------------------------------------------------------------


def test_registry_ordering_is_deterministic_regardless_of_entry_point_order() -> None:
    forward = [
        entry_point("good", "fixtures:GoodConnector"),
        entry_point("another_good", "fixtures:AnotherGoodConnector"),
    ]
    backward = list(reversed(forward))

    assert discover_connectors(forward).names() == ("another_good", "good")
    assert discover_connectors(backward).names() == ("another_good", "good")
