"""SyncPairConfig: the sync-pair schema — defaults, validation messages, and the
catalog.schema glob matcher.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qlabs_catalog_sync.config import (
    ManualEditMode,
    SyncPairConfig,
    matches_catalog_schema,
)
from qlabs_catalog_sync_sdk.models import EntityType


def _valid_pair_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "name": "databricks_prod_to_qlik_acme_sales",
        "source": "databricks_prod",
        "target": "qlik_acme",
        "catalog_schema_patterns": ["prod_catalog.sales_*"],
        "target_space": "Data Products - Sales",
        "entity_types": [EntityType.DATA_PRODUCT, EntityType.DATASET],
    }
    kwargs.update(overrides)
    return kwargs


def test_valid_pair_loads_with_expected_defaults() -> None:
    pair = SyncPairConfig(**_valid_pair_kwargs())

    assert pair.cadence_seconds == 900
    assert pair.manual_edit_policy.default == ManualEditMode.SOURCE_WINS
    assert pair.manual_edit_policy.per_entity == {}
    assert pair.manual_edit_policy.per_field == {}


def test_activation_opt_in_defaults_to_off() -> None:
    pair = SyncPairConfig(**_valid_pair_kwargs())
    assert pair.activation_opt_in is False


def test_activation_opt_in_can_be_enabled_explicitly() -> None:
    pair = SyncPairConfig(**_valid_pair_kwargs(activation_opt_in=True))
    assert pair.activation_opt_in is True


@pytest.mark.parametrize(
    "pattern",
    [
        "sales_star_no_dot",  # no '.' at all
        "sales.finance.extra",  # two dots
        ".sales",  # empty catalog segment
        "sales.",  # empty schema segment
        "sa les.finance",  # space is not an allowed segment character
        "sales.fin/ance",  # slash is not an allowed segment character
    ],
)
def test_invalid_catalog_schema_pattern_names_pair_and_field(pattern: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        SyncPairConfig(**_valid_pair_kwargs(catalog_schema_patterns=[pattern]))

    message = str(exc_info.value)
    assert "databricks_prod_to_qlik_acme_sales" in message
    assert "catalog_schema_patterns" in message
    assert pattern in message


def test_empty_catalog_schema_patterns_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        SyncPairConfig(**_valid_pair_kwargs(catalog_schema_patterns=[]))

    message = str(exc_info.value)
    assert "databricks_prod_to_qlik_acme_sales" in message
    assert "catalog_schema_patterns" in message


def test_missing_catalog_schema_patterns_is_rejected() -> None:
    kwargs = _valid_pair_kwargs()
    del kwargs["catalog_schema_patterns"]

    with pytest.raises(ValidationError) as exc_info:
        SyncPairConfig(**kwargs)

    message = str(exc_info.value)
    assert "databricks_prod_to_qlik_acme_sales" in message
    assert "catalog_schema_patterns" in message


@pytest.mark.parametrize("cadence", [0, -1, -900])
def test_non_positive_cadence_names_pair_and_field(cadence: int) -> None:
    with pytest.raises(ValidationError) as exc_info:
        SyncPairConfig(**_valid_pair_kwargs(cadence_seconds=cadence))

    message = str(exc_info.value)
    assert "databricks_prod_to_qlik_acme_sales" in message
    assert "cadence_seconds" in message
    assert "must be positive" in message


def test_blank_target_space_names_pair_and_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        SyncPairConfig(**_valid_pair_kwargs(target_space="   "))

    message = str(exc_info.value)
    assert "databricks_prod_to_qlik_acme_sales" in message
    assert "target_space" in message


def test_missing_target_space_names_pair_and_field() -> None:
    kwargs = _valid_pair_kwargs()
    del kwargs["target_space"]

    with pytest.raises(ValidationError) as exc_info:
        SyncPairConfig(**kwargs)

    message = str(exc_info.value)
    assert "databricks_prod_to_qlik_acme_sales" in message
    assert "target_space" in message


def test_empty_entity_types_names_pair_and_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        SyncPairConfig(**_valid_pair_kwargs(entity_types=[]))

    message = str(exc_info.value)
    assert "databricks_prod_to_qlik_acme_sales" in message
    assert "entity_types" in message


def test_unsupported_entity_type_is_rejected_with_valid_choices() -> None:
    # "principal" is not, and by design never will be, a neutral EntityType (v1 has no
    # access-control sync) — pydantic's own enum coercion rejects it, and the message
    # already names the field location and lists the valid choices.
    with pytest.raises(ValidationError) as exc_info:
        SyncPairConfig(**_valid_pair_kwargs(entity_types=["principal"]))

    message = str(exc_info.value)
    assert "entity_types" in message
    assert "data_product" in message  # one of the valid choices is listed


class TestMatchesCatalogSchema:
    def test_exact_match(self) -> None:
        assert matches_catalog_schema("sales.public", "sales", "public") is True

    def test_wildcard_schema_matches(self) -> None:
        assert matches_catalog_schema("sales.*", "sales", "anything_at_all") is True

    def test_wildcard_catalog_matches(self) -> None:
        assert matches_catalog_schema("*.finance", "prod_catalog", "finance") is True

    def test_prefix_glob_matches(self) -> None:
        assert matches_catalog_schema("prod_*.sales_*", "prod_catalog", "sales_eu") is True

    def test_case_sensitive_no_match(self) -> None:
        assert matches_catalog_schema("sales.public", "Sales", "public") is False

    def test_different_catalog_does_not_match(self) -> None:
        assert matches_catalog_schema("sales.public", "marketing", "public") is False

    def test_wildcard_does_not_match_across_segments(self) -> None:
        # "sales.*" must not match a schema in a *different* catalog just because the
        # schema half is a wildcard.
        assert matches_catalog_schema("sales.*", "marketing", "anything") is False

    def test_pair_matches_delegates_to_any_pattern(self) -> None:
        pair = SyncPairConfig(
            **_valid_pair_kwargs(
                catalog_schema_patterns=["marketing.campaigns", "prod_catalog.sales_*"]
            )
        )
        assert pair.matches("prod_catalog", "sales_eu") is True
        assert pair.matches("marketing", "campaigns") is True
        assert pair.matches("prod_catalog", "hr") is False
