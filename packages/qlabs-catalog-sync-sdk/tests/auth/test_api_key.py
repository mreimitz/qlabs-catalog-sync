"""ApiKeyAuthProvider: a static bearer token, no network involved."""

from __future__ import annotations

from qlabs_catalog_sync_sdk.auth import ApiKeyAuthProvider


async def test_headers_returns_bearer_token() -> None:
    provider = ApiKeyAuthProvider("sk-live-secretvalue")

    headers = await provider.headers()

    assert headers == {"Authorization": "Bearer sk-live-secretvalue"}


async def test_headers_is_stable_across_calls() -> None:
    # Databricks PATs and Qlik API keys never expire from the provider's point
    # of view, so repeated calls just return the same value.
    provider = ApiKeyAuthProvider("sk-live-secretvalue")

    first = await provider.headers()
    second = await provider.headers()

    assert first == second


async def test_custom_header_and_scheme() -> None:
    provider = ApiKeyAuthProvider("token-123", header_name="X-Api-Key", scheme=None)

    headers = await provider.headers()

    assert headers == {"X-Api-Key": "token-123"}
