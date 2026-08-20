"""OAuth2ClientCredentialsProvider: caching, refresh-before-expiry, concurrent
callers sharing one fetch, failure handling, and both wire encodings (Qlik's
JSON body and Databricks' form-encoded + HTTP Basic)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from qlabs_catalog_sync_sdk.auth import (
    AuthError,
    OAuth2ClientCredentialsProvider,
    TokenRequestEncoding,
)

TOKEN_URL = "https://tenant.eu.qlikcloud.example/oauth/token"


def _provider(*, transport, clock, encoding=TokenRequestEncoding.JSON, **kwargs):
    return OAuth2ClientCredentialsProvider(
        token_url=TOKEN_URL,
        client_id="client-abc",
        client_secret="s3cr3t-value",
        transport=transport,
        encoding=encoding,
        clock=clock,
        **kwargs,
    )


async def test_token_is_fetched_once_and_reused_while_valid(respx_mock, transport, clock):
    route = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    provider = _provider(transport=transport, clock=clock)

    first = await provider.headers()
    second = await provider.headers()

    assert first == {"Authorization": "Bearer tok-1"}
    assert second == first
    assert route.call_count == 1


async def test_token_is_refreshed_before_expiry_not_after_a_401(respx_mock, transport, clock):
    route = respx_mock.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600}),
        ]
    )
    provider = _provider(transport=transport, clock=clock, refresh_margin=timedelta(seconds=60))

    first = await provider.headers()
    assert first == {"Authorization": "Bearer tok-1"}
    assert route.call_count == 1

    # Not yet within the refresh margin: still the cached token, no new request.
    clock.advance(3600 - 61)
    still_cached = await provider.headers()
    assert still_cached == first
    assert route.call_count == 1

    # Now within the refresh margin but the token has NOT expired yet (no 401
    # ever happens in this test) — the provider refreshes proactively.
    clock.advance(30)
    refreshed = await provider.headers()
    assert refreshed == {"Authorization": "Bearer tok-2"}
    assert route.call_count == 2


async def test_concurrent_headers_calls_trigger_exactly_one_token_request(
    respx_mock, transport, clock
):
    route = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    provider = _provider(transport=transport, clock=clock)

    results = await asyncio.gather(
        provider.headers(), provider.headers(), provider.headers()
    )

    assert all(r == {"Authorization": "Bearer tok-1"} for r in results)
    assert route.call_count == 1


async def test_failed_token_exchange_raises_and_is_not_cached(respx_mock, transport, clock):
    route = respx_mock.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(401, json={"error": "invalid_client"}),
            httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600}),
        ]
    )
    provider = _provider(transport=transport, clock=clock)

    with pytest.raises(AuthError):
        await provider.headers()
    assert route.call_count == 1

    # The failure was not cached: the next call retries against the token
    # endpoint rather than reusing (or being poisoned by) the failed attempt.
    headers = await provider.headers()
    assert headers == {"Authorization": "Bearer tok-1"}
    assert route.call_count == 2


async def test_malformed_token_response_raises_and_is_not_cached(respx_mock, transport, clock):
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"token_type": "bearer"}))
    provider = _provider(transport=transport, clock=clock)

    with pytest.raises(AuthError):
        await provider.headers()


async def test_json_encoding_posts_credentials_in_body(respx_mock, transport, clock):
    route = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    provider = _provider(
        transport=transport, clock=clock, encoding=TokenRequestEncoding.JSON, scope="user_default"
    )

    await provider.headers()

    request = route.calls.last.request
    assert request.headers.get("content-type", "").startswith("application/json")
    assert "authorization" not in {k.lower() for k in request.headers}
    body = json.loads(request.content)
    assert body == {
        "grant_type": "client_credentials",
        "client_id": "client-abc",
        "client_secret": "s3cr3t-value",
        "scope": "user_default",
    }


async def test_form_encoding_uses_http_basic_auth(respx_mock, transport, clock):
    route = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    provider = _provider(
        transport=transport, clock=clock, encoding=TokenRequestEncoding.FORM, scope="all-apis"
    )

    await provider.headers()

    request = route.calls.last.request
    assert request.headers.get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    )
    auth_header = request.headers.get("authorization", "")
    assert auth_header.startswith("Basic ")
    # Credentials travel as Basic auth, never as body fields, for the Databricks-
    # shaped request.
    form = parse_qs(request.content.decode())
    assert form == {"grant_type": ["client_credentials"], "scope": ["all-apis"]}
    assert "client_id" not in form
    assert "client_secret" not in form
