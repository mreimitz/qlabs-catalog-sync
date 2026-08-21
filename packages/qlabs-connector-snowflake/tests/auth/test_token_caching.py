"""Key-pair JWT token caching: mint lazily, cache, and refresh before expiry with a
safety margin — the SDK's ``AuthProvider``/``JWTAuthProvider`` behavior, proven here for
Snowflake's specific issuer/subject/lifetime wiring with a fake clock (no real sleeping).
"""

from __future__ import annotations

from datetime import timedelta

from qlabs_catalog_sync_sdk.auth import DEFAULT_REFRESH_MARGIN
from qlabs_connector_snowflake.auth import build_key_pair_jwt_provider

from .conftest import build_config


async def test_token_is_not_minted_until_first_use(rsa_keypair, clock) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)

    # Building the provider does no signing — only calling headers()/_token() does.
    build_key_pair_jwt_provider(config, endpoint="snowflake", clock=clock)  # must not raise


async def test_token_is_reused_while_still_fresh(rsa_keypair, clock) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)
    provider = build_key_pair_jwt_provider(
        config, endpoint="snowflake", lifetime=timedelta(minutes=30), clock=clock
    )

    first = await provider.headers()
    clock.advance(60)  # well inside the 30-minute lifetime and the refresh margin
    second = await provider.headers()

    assert first == second


async def test_token_is_refreshed_once_inside_the_margin_before_expiry(rsa_keypair, clock) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)
    lifetime = timedelta(minutes=30)
    provider = build_key_pair_jwt_provider(
        config, endpoint="snowflake", lifetime=lifetime, clock=clock
    )

    first = await provider.headers()

    # Move to just inside the default refresh margin before the token's expiry.
    seconds_to_margin = (lifetime - DEFAULT_REFRESH_MARGIN).total_seconds() + 1
    clock.advance(seconds_to_margin)
    second = await provider.headers()

    assert second != first


async def test_a_still_fresh_token_survives_a_second_provider_call_with_no_clock_movement(
    rsa_keypair, clock
) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)
    provider = build_key_pair_jwt_provider(config, endpoint="snowflake", clock=clock)

    first = await provider.headers()
    second = await provider.headers()

    assert first == second
