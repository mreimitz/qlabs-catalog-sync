"""Personal access token (PAT) as an alternative to the OAuth M2M service principal.

Databricks calls PATs legacy and recommends OAuth, and this connector agrees -- OAuth
stays the documented default. But a PAT is one click in the workspace UI where a service
principal needs a workspace admin, and an operator standing up an endpoint for the first
time should not need the admin before they can see anything work.

Nothing downstream changes, and that is the point of these tests as much as the feature:
``default_workspace_client_factory`` already builds its ``WorkspaceClient`` with
``auth_type="pat"`` and a bare bearer token, and the SDK already ships
``ApiKeyAuthProvider`` (whose own docstring names Databricks PATs). OAuth was only ever
the thing *producing* that bearer token, so configuring a PAT short-circuits the token
fetch and leaves reads, tag queries and the Statement Execution path untouched.

The assertions that matter most here are the negative ones: that the PAT path never calls
the OIDC token endpoint (``respx`` fails the test if it does), and that the token never
surfaces in a repr, a log or a validation error.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from qlabs_catalog_sync_sdk.auth import ApiKeyAuthProvider, OAuth2ClientCredentialsProvider
from qlabs_catalog_sync_sdk.contract import HealthState
from qlabs_connector_databricks import Connector
from qlabs_connector_databricks.auth import build_auth_provider
from qlabs_connector_databricks.config import DatabricksConfig

from .conftest import (
    TOKEN_URL,
    RecordingWorkspaceClientFactory,
    build_config,
    build_ctx,
    build_pat_config,
)

MARKER = "MARKER-PAT-9c31fe"


# --------------------------------------------------------------------------------------
# Config: exactly one of the two credential routes
# --------------------------------------------------------------------------------------


def test_a_token_only_config_is_valid() -> None:
    config = build_pat_config()

    assert config.token is not None
    assert config.token.get_secret_value() == "dapi-personal-access-token"
    assert config.client_id is None
    assert config.client_secret is None


def test_an_oauth_only_config_is_still_valid() -> None:
    """The default route must not become collateral damage of adding the alternative."""
    config = build_config()

    assert config.token is None
    assert config.client_id == "sp-client-abc"


def test_configuring_both_routes_is_refused() -> None:
    """No silent precedence (decided with the operator): a leftover ``client_secret``
    must never quietly override the PAT somebody just added, and vice versa."""
    with pytest.raises(ValidationError) as exc_info:
        DatabricksConfig(
            host="https://acme.cloud.databricks.com",
            client_id="sp-client-abc",
            client_secret="sp-secret-value",
            token="dapi-personal-access-token",
        )

    message = str(exc_info.value)
    assert "client_id" in message
    assert "token" in message


def test_configuring_neither_route_is_refused() -> None:
    with pytest.raises(ValidationError) as exc_info:
        DatabricksConfig(host="https://acme.cloud.databricks.com")

    message = str(exc_info.value)
    assert "client_id" in message
    assert "token" in message


def test_a_refused_config_never_echoes_the_token() -> None:
    """A validation error is the single most likely place a secret gets printed: it is
    raised on the operator's screen, and pydantic quotes input values by default."""
    with pytest.raises(ValidationError) as exc_info:
        DatabricksConfig(
            host="https://acme.cloud.databricks.com",
            client_id="sp-client-abc",
            client_secret="sp-secret-value",
            token=MARKER,
        )

    assert MARKER not in str(exc_info.value)


def test_the_token_is_not_readable_from_the_config_repr() -> None:
    config = build_pat_config(token=MARKER)

    assert MARKER not in repr(config)
    assert MARKER not in str(config)


# --------------------------------------------------------------------------------------
# Provider selection
# --------------------------------------------------------------------------------------


async def test_a_token_config_yields_a_static_bearer_provider(transport) -> None:
    provider = build_auth_provider(build_pat_config(), transport=transport)

    assert isinstance(provider, ApiKeyAuthProvider)
    assert await provider.headers() == {"Authorization": "Bearer dapi-personal-access-token"}


def test_an_oauth_config_still_yields_the_oauth_provider(transport) -> None:
    provider = build_auth_provider(build_config(), transport=transport)

    assert isinstance(provider, OAuth2ClientCredentialsProvider)


async def test_the_token_provider_never_calls_the_oidc_endpoint(respx_mock, transport) -> None:
    """``respx_mock`` asserts all mocked routes are called and refuses unmocked requests,
    so a token-endpoint call on this path fails the test rather than passing silently."""
    route = respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(500))

    provider = build_auth_provider(build_pat_config(), transport=transport)
    await provider.headers()

    assert not route.called


# --------------------------------------------------------------------------------------
# End to end through setup() and healthcheck()
# --------------------------------------------------------------------------------------


async def test_setup_and_healthcheck_work_on_a_token(respx_mock, transport) -> None:
    """The whole point: an endpoint configured with nothing but a host and a PAT reaches
    a healthy Unity Catalog probe."""
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(500))
    factory = RecordingWorkspaceClientFactory()
    connector = Connector(workspace_client_factory=factory, transport=transport)

    await connector.setup(build_ctx(build_pat_config()))
    report = await connector.healthcheck()

    assert report.state is HealthState.HEALTHY
    # setup() builds a client and healthcheck() rebuilds it; both must have been handed
    # the PAT itself as the bearer token, with no token exchange in between.
    expected = ("https://acme.cloud.databricks.com", "dapi-personal-access-token")
    assert factory.calls == [expected, expected]


async def test_the_token_never_appears_in_the_connector_or_provider_repr(
    respx_mock, transport
) -> None:
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(500))
    connector = Connector(
        workspace_client_factory=RecordingWorkspaceClientFactory(), transport=transport
    )

    await connector.setup(build_ctx(build_pat_config(token=MARKER)))

    assert MARKER not in repr(connector)
    assert MARKER not in str(connector)
    assert connector._token_provider is not None
    assert MARKER not in repr(connector._token_provider)
    assert MARKER not in str(connector._token_provider)
