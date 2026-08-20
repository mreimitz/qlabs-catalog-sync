"""EntityCapability: identity_keys, allowed_update_paths, and the operation cap.

Section 2 of the RS-02 qlik-two-way-sync-readiness note is the reference for the exact
Qlik data-product enum this module has to be able to express: JSON Patch ``op:
"replace"`` only, a closed 8-path enum, arrays sent as a full replace, max 8 operations
per request.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qlabs_catalog_sync_sdk.manifest import EntityCapability, FieldCapability

QLIK_DATA_PRODUCT_PATHS = [
    "/name",
    "/description",
    "/datasetIds",
    "/glossaryIds",
    "/readMe",
    "/keyContacts",
    "/tags",
    "/apiConsumableDatasetIds",
]


def test_supported_entity_requires_identity_keys() -> None:
    with pytest.raises(ValidationError, match="identity_keys"):
        EntityCapability(supported=True, identity_keys=[])


def test_unsupported_entity_does_not_require_identity_keys() -> None:
    capability = EntityCapability(supported=False)

    assert capability.identity_keys == []


def test_identity_keys_must_not_repeat() -> None:
    with pytest.raises(ValidationError, match="repeat"):
        EntityCapability(supported=True, identity_keys=["id", "id"])


def test_allowed_update_paths_cannot_be_an_empty_list() -> None:
    with pytest.raises(ValidationError, match="omitted"):
        EntityCapability(supported=True, identity_keys=["id"], allowed_update_paths=[])


def test_allowed_update_paths_must_be_json_pointers() -> None:
    with pytest.raises(ValidationError, match="JSON Pointers"):
        EntityCapability(supported=True, identity_keys=["id"], allowed_update_paths=["name"])


def test_allowed_update_paths_must_not_repeat() -> None:
    with pytest.raises(ValidationError, match="repeat"):
        EntityCapability(
            supported=True, identity_keys=["id"], allowed_update_paths=["/name", "/name"]
        )


def test_the_qlik_data_product_enum_is_exactly_eight_paths_capped_at_eight_ops() -> None:
    capability = EntityCapability(
        supported=True,
        identity_keys=["id", "qri"],
        fields={"name": FieldCapability.rw(writable_via="rest-patch")},
        allowed_update_paths=list(QLIK_DATA_PRODUCT_PATHS),
        max_update_operations=8,
    )

    assert capability.allowed_update_paths == QLIK_DATA_PRODUCT_PATHS
    assert len(capability.allowed_update_paths) == 8
    assert capability.max_update_operations == 8


def test_max_update_operations_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError, match="at least 1"):
        EntityCapability(
            supported=True,
            identity_keys=["id"],
            allowed_update_paths=["/name"],
            max_update_operations=0,
        )


def test_max_update_operations_cannot_exceed_the_path_enum() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        EntityCapability(
            supported=True,
            identity_keys=["id"],
            allowed_update_paths=["/name"],
            max_update_operations=2,
        )


def test_allowed_update_paths_default_to_none_meaning_no_closed_enum() -> None:
    capability = EntityCapability(supported=True, identity_keys=["id"])

    assert capability.allowed_update_paths is None
    assert capability.max_update_operations is None


def test_supports_events_defaults_false() -> None:
    assert EntityCapability(supported=True, identity_keys=["id"]).supports_events is False


def test_round_trips_through_json() -> None:
    capability = EntityCapability(
        supported=True,
        identity_keys=["id", "qri"],
        fields={
            "name": FieldCapability.rw(writable_via="rest-patch"),
            "placement": FieldCapability.ro(),
            "glossary_term_refs": FieldCapability.na(),
        },
        allowed_update_paths=list(QLIK_DATA_PRODUCT_PATHS),
        max_update_operations=8,
    )

    restored = EntityCapability.model_validate(capability.model_dump(mode="json"))

    assert restored == capability
