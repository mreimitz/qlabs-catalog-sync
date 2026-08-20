"""No provider ever leaks a secret or a live token via repr()/str(), an
exception message, or disk I/O.

Each test plants a distinctive marker string as the secret and asserts the
marker never resurfaces anywhere observable.
"""

from __future__ import annotations

import inspect

import httpx
import pytest

from qlabs_catalog_sync_sdk import auth
from qlabs_catalog_sync_sdk.auth import (
    ApiKeyAuthProvider,
    AuthError,
    JWTAuthProvider,
    OAuth2ClientCredentialsProvider,
    TokenRequestEncoding,
)

API_KEY_MARKER = "MARKER-API-KEY-6f1a"
CLIENT_SECRET_MARKER = "MARKER-CLIENT-SECRET-9c2b"
TOKEN_URL = "https://tenant.eu.qlikcloud.example/oauth/token"


def test_api_key_never_appears_in_repr_or_str() -> None:
    provider = ApiKeyAuthProvider(API_KEY_MARKER)

    assert API_KEY_MARKER not in repr(provider)
    assert API_KEY_MARKER not in str(provider)


async def test_api_key_never_appears_after_use(clock) -> None:
    # Fetching (and thus caching) the token must not change what repr()/str()
    # reveal either.
    provider = ApiKeyAuthProvider(API_KEY_MARKER, clock=clock)
    await provider.headers()

    assert API_KEY_MARKER not in repr(provider)
    assert API_KEY_MARKER not in str(provider)
    assert provider._cached is not None
    assert API_KEY_MARKER not in repr(provider._cached)
    assert API_KEY_MARKER not in str(provider._cached)


def test_oauth2_client_secret_never_appears_in_repr_or_str(transport, clock) -> None:
    provider = OAuth2ClientCredentialsProvider(
        token_url=TOKEN_URL,
        client_id="client-abc",
        client_secret=CLIENT_SECRET_MARKER,
        transport=transport,
        clock=clock,
    )

    assert CLIENT_SECRET_MARKER not in repr(provider)
    assert CLIENT_SECRET_MARKER not in str(provider)
    # client_id is not a secret and is useful for debugging; it may appear.
    assert "client-abc" in repr(provider)


async def test_oauth2_fetched_token_never_appears_in_repr(respx_mock, transport, clock) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "MARKER-ACCESS-TOKEN-77aa", "expires_in": 3600}
        )
    )
    provider = OAuth2ClientCredentialsProvider(
        token_url=TOKEN_URL,
        client_id="client-abc",
        client_secret=CLIENT_SECRET_MARKER,
        transport=transport,
        clock=clock,
    )

    await provider.headers()

    assert "MARKER-ACCESS-TOKEN-77aa" not in repr(provider)
    assert "MARKER-ACCESS-TOKEN-77aa" not in str(provider)


async def test_oauth2_failure_message_never_contains_the_secret(
    respx_mock, transport, clock
) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    provider = OAuth2ClientCredentialsProvider(
        token_url=TOKEN_URL,
        client_id="client-abc",
        client_secret=CLIENT_SECRET_MARKER,
        transport=transport,
        encoding=TokenRequestEncoding.FORM,
        clock=clock,
    )

    with pytest.raises(AuthError) as exc_info:
        await provider.headers()

    assert CLIENT_SECRET_MARKER not in str(exc_info.value)
    assert CLIENT_SECRET_MARKER not in repr(exc_info.value)


def test_jwt_private_key_never_appears_in_repr_or_str(rsa_keypair, clock) -> None:
    provider = JWTAuthProvider(
        private_key=rsa_keypair.private_pem,
        issuer="iss",
        clock=clock,
    )

    assert rsa_keypair.private_pem not in repr(provider)
    assert rsa_keypair.private_pem not in str(provider)
    assert "PRIVATE KEY" not in repr(provider)
    assert "PRIVATE KEY" not in str(provider)


async def test_jwt_signed_assertion_never_appears_in_repr(rsa_keypair, clock) -> None:
    provider = JWTAuthProvider(private_key=rsa_keypair.private_pem, issuer="iss", clock=clock)

    headers = await provider.headers()
    token = headers["Authorization"].removeprefix("Bearer ")

    assert token not in repr(provider)
    assert token not in str(provider)


def test_no_disk_io_in_auth_module() -> None:
    """Static proxy for "tokens are never persisted": scan the module source
    for any file-write-shaped call. A change that starts writing tokens (or
    anything else) to disk from this module should fail this test."""
    source = inspect.getsource(auth)
    disk_write_markers = [
        "open(",
        ".write_text(",
        ".write_bytes(",
        "pickle.dump",
        "shelve.",
        "sqlite3.",
    ]
    for marker in disk_write_markers:
        assert marker not in source, f"unexpected disk I/O marker {marker!r} in auth.py"
