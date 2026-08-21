"""Shared fixtures for the Qlik connector's auth/setup/healthcheck tests.

Everything here builds a *real* ``QlikConfig`` and a *real* ``Connector`` — nothing is
patched out — and drives it against ``respx``-mocked HTTP, exactly like the SDK's own
auth and http test suites (``qlabs_catalog_sync_sdk/tests/auth``,
``qlabs_catalog_sync_sdk/tests/http``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from qlabs_catalog_sync_sdk.config import ConnectorContext, ManualClock
from qlabs_connector_qlik import Connector
from qlabs_connector_qlik.auth import build_http_endpoint
from qlabs_connector_qlik.config import QlikConfig

TENANT_BASE_URL = "https://acme.eu.qlikcloud.example"
TOKEN_URL = f"{TENANT_BASE_URL}/oauth/token"
ENDPOINT = "qlik"
SPACE_ID = "space-123"


@pytest.fixture
def clock() -> ManualClock:
    """A clock that only moves when told to — proves refresh-before-expiry and lets
    retry-after waits resolve instantly instead of really sleeping."""
    return ManualClock()


@pytest.fixture
def qlik_config() -> QlikConfig:
    return QlikConfig(
        base_url=TENANT_BASE_URL,
        client_id="client-abc",
        client_secret="s3cr3t-value",
        scope="user_default",
        space_id=SPACE_ID,
    )


@pytest.fixture
def context(qlik_config: QlikConfig, clock: ManualClock) -> ConnectorContext[QlikConfig]:
    return ConnectorContext.build(config=qlik_config, endpoint=ENDPOINT, tenant="acme", clock=clock)


@pytest.fixture
async def connector(context: ConnectorContext[QlikConfig]) -> AsyncIterator[Connector]:
    conn = Connector()
    await conn.setup(context)
    yield conn
    await conn.close()


@pytest.fixture
async def make_connector(
    qlik_config: QlikConfig, clock: ManualClock
) -> AsyncIterator[Callable[..., Connector]]:
    """Factory for a ``Connector`` wired with overridable ``HttpEndpoint`` settings —
    for tests that need to exhaust retries (429/5xx/transport) fast and deterministically
    rather than waiting out the real default backoff schedule. Mirrors the SDK's own
    ``make_endpoint`` factory fixture (``qlabs_catalog_sync_sdk/tests/http/conftest.py``).

    Bypasses ``Connector.setup()``'s fixed call to ``build_http_endpoint`` only to forward
    extra ``HttpEndpoint`` kwargs it does not itself accept; everything it wires
    (``ctx``, ``http``, the OAuth2 provider, the token client) is otherwise identical to
    what a real ``setup()`` call produces.
    """
    made: list[Connector] = []

    def _make(**http_kwargs: object) -> Connector:
        ctx = ConnectorContext.build(
            config=qlik_config, endpoint=ENDPOINT, tenant="acme", clock=clock
        )
        conn = Connector()
        http, oauth_provider, token_client = build_http_endpoint(
            qlik_config, clock=clock, **http_kwargs
        )
        conn.ctx = ctx
        conn.http = http
        conn._oauth_provider = oauth_provider
        conn._token_client = token_client
        made.append(conn)
        return conn

    yield _make
    for conn in made:
        await conn.close()


@pytest.fixture
def mock_token(respx_mock: object) -> Callable[..., object]:
    """Mock a successful Qlik OAuth2 token exchange. Call with no args for a default
    1-hour token, or override ``access_token``/``expires_in``/``status_code``."""

    def _mock(
        *,
        access_token: str = "tok-1",
        expires_in: int = 3600,
        status_code: int = 200,
    ) -> object:
        if status_code == 200:
            response = httpx.Response(
                status_code, json={"access_token": access_token, "expires_in": expires_in}
            )
        else:
            response = httpx.Response(status_code, json={"error": "invalid_client"})
        return respx_mock.post(TOKEN_URL).mock(return_value=response)  # type: ignore[attr-defined]

    return _mock
