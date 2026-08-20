"""Every ``ConfigService`` (T10.3) and connector-discovery (WP2) typed error this API
maps gets a test asserting both the HTTP status code **and** the
:class:`~qlabs_catalog_sync.api.errors.ErrorModel` body -- not just the status. A route
never hand-writes a status code per exception (``api/errors.py``'s whole point); these
tests are what proves the mapping table itself is right, using a throwaway route per
exception since the real business routes that raise these land with T12.3 onward.

Also asserts the error model is *declared* in the generated OpenAPI schema (T12.8 reads
it to generate the TypeScript client) -- a mapping that only exists at runtime and never
appears in ``openapi.json`` would give the console an untyped ``catch``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from qlabs_catalog_sync.api.errors import ErrorModel
from qlabs_catalog_sync.configstore.secrets import SecretRefFormatError
from qlabs_catalog_sync.configstore.service import (
    ConfigServiceError,
    EndpointAlreadyExistsError,
    EndpointInUseError,
    EndpointNotFoundError,
    EndpointSettingsValidationError,
    InlineSecretRejectedError,
    SelectionOverrideAlreadyExistsError,
    SelectionOverrideNotFoundError,
    SelectionRuleNotFoundError,
    SelectionRuleOrdinalConflictError,
    SelectionRuleReorderMismatchError,
    SyncPairAlreadyExistsError,
    SyncPairEndpointError,
    SyncPairNotFoundError,
)
from qlabs_catalog_sync.configstore.types import RuleScope
from qlabs_catalog_sync.discovery import (
    BrokenConnector,
    ConnectorBrokenError,
    ConnectorNotRegisteredError,
)

from .api_helpers import add_raising_route, build_app

_PAIR_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_RULE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OVERRIDE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

#: (case name, exception factory, expected status, expected code, expected field,
#: expected entity). ``None`` means "must be exactly ``None``", not "don't care".
_CASES: list[tuple[str, Callable[[], BaseException], int, str, str | None, str | None]] = [
    (
        "endpoint_not_found",
        lambda: EndpointNotFoundError("qlik_acme"),
        404,
        "endpoint_not_found",
        None,
        "qlik_acme",
    ),
    (
        "endpoint_already_exists",
        lambda: EndpointAlreadyExistsError("qlik_acme"),
        409,
        "endpoint_already_exists",
        None,
        "qlik_acme",
    ),
    (
        "endpoint_in_use",
        lambda: EndpointInUseError("qlik_acme", pairs=["db-to-qlik"]),
        409,
        "endpoint_in_use",
        None,
        "qlik_acme",
    ),
    (
        "inline_secret_rejected",
        lambda: InlineSecretRejectedError("qlik", fields=["api_key"]),
        422,
        "inline_secret_rejected",
        "api_key",
        "qlik",
    ),
    (
        "endpoint_settings_invalid",
        lambda: EndpointSettingsValidationError("qlik", "space_id: field required"),
        422,
        "endpoint_settings_invalid",
        None,
        "qlik",
    ),
    (
        "sync_pair_not_found",
        lambda: SyncPairNotFoundError(_PAIR_ID),
        404,
        "sync_pair_not_found",
        None,
        str(_PAIR_ID),
    ),
    (
        "sync_pair_already_exists",
        lambda: SyncPairAlreadyExistsError("db-to-qlik"),
        409,
        "sync_pair_already_exists",
        None,
        "db-to-qlik",
    ),
    (
        "sync_pair_endpoint_invalid",
        lambda: SyncPairEndpointError("target must be the sole write connector"),
        422,
        "sync_pair_endpoint_invalid",
        None,
        None,
    ),
    (
        "selection_rule_not_found",
        lambda: SelectionRuleNotFoundError(_RULE_ID),
        404,
        "selection_rule_not_found",
        None,
        str(_RULE_ID),
    ),
    (
        "selection_rule_ordinal_conflict",
        lambda: SelectionRuleOrdinalConflictError(_PAIR_ID, RuleScope.OBJECT, 3),
        409,
        "selection_rule_ordinal_conflict",
        "ordinal",
        str(_PAIR_ID),
    ),
    (
        "selection_rule_reorder_mismatch",
        lambda: SelectionRuleReorderMismatchError(
            _PAIR_ID, RuleScope.OBJECT, expected=["a", "b"], given=["a"]
        ),
        422,
        "selection_rule_reorder_mismatch",
        None,
        str(_PAIR_ID),
    ),
    (
        "selection_override_not_found",
        lambda: SelectionOverrideNotFoundError(_OVERRIDE_ID),
        404,
        "selection_override_not_found",
        None,
        str(_OVERRIDE_ID),
    ),
    (
        "selection_override_already_exists",
        lambda: SelectionOverrideAlreadyExistsError(
            _PAIR_ID, RuleScope.DATASET, "acme.sales.orders"
        ),
        409,
        "selection_override_already_exists",
        None,
        str(_PAIR_ID),
    ),
    (
        "connector_not_registered",
        lambda: ConnectorNotRegisteredError("databricks", available=("qlik",)),
        422,
        "connector_not_registered",
        "connector",
        "databricks",
    ),
    (
        "connector_broken",
        lambda: ConnectorBrokenError(
            BrokenConnector(
                name="databricks",
                distribution="qlabs-connector-databricks",
                stage="load",
                reason="ImportError: boom",
            )
        ),
        503,
        "connector_broken",
        None,
        "databricks",
    ),
    (
        "secret_ref_invalid",
        lambda: SecretRefFormatError("vault:kv/x", "unsupported scheme"),
        422,
        "secret_ref_invalid",
        "secret_ref",
        None,
    ),
]


@pytest.mark.parametrize(
    (
        "case_name",
        "make_exc",
        "expected_status",
        "expected_code",
        "expected_field",
        "expected_entity",
    ),
    _CASES,
    ids=[case[0] for case in _CASES],
)
def test_typed_error_maps_to_status_and_error_model(
    case_name: str,
    make_exc: Callable[[], BaseException],
    expected_status: int,
    expected_code: str,
    expected_field: str | None,
    expected_entity: str | None,
) -> None:
    app = build_app()
    add_raising_route(app, f"/__test/{case_name}", make_exc)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"/__test/{case_name}")

    assert response.status_code == expected_status
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["code"] == expected_code
    assert isinstance(body["message"], str) and body["message"]
    assert body["field"] == expected_field
    assert body["entity"] == expected_entity
    assert body["correlation_id"] is None  # only the generic-500 path sets this


def test_unmapped_config_service_error_subclass_still_gets_a_deliberate_422_not_500() -> None:
    """The base-class safety net (``api/errors.py``'s tier 2): a future
    ``ConfigServiceError`` subclass nobody has written a specific handler for yet must
    still come back as a domain 422, never fall through to the generic 500."""

    class _FutureConfigServiceError(ConfigServiceError):
        pass

    app = build_app()
    add_raising_route(
        app,
        "/__test/future-config-error",
        lambda: _FutureConfigServiceError("new kind of failure"),
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/__test/future-config-error")

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "config_service_error"
    assert "new kind of failure" in body["message"]


def test_error_model_is_declared_in_the_generated_openapi_schema() -> None:
    """T12.8 generates a TypeScript client from ``openapi.json``; if ``ErrorModel``
    never appears there, every route's error path is an untyped ``catch`` in the
    console."""
    app = build_app()
    client = TestClient(app)

    schema = client.get("/openapi.json").json()

    assert "ErrorModel" in schema["components"]["schemas"]
    error_schema = schema["components"]["schemas"]["ErrorModel"]
    assert set(ErrorModel.model_fields) <= set(error_schema["properties"])
