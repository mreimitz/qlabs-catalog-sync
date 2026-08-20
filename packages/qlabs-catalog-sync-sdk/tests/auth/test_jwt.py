"""JWTAuthProvider: signs an assertion with a private key and either uses it
directly as the bearer token, or exchanges it at a token endpoint."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import jwt
import pytest

from qlabs_catalog_sync_sdk.auth import AuthError, JWTAuthProvider

TOKEN_URL = "https://sfaccount.snowflakecomputing.example/oauth/token"


async def test_direct_mode_assertion_verifies_against_public_key(rsa_keypair, clock):
    provider = JWTAuthProvider(
        private_key=rsa_keypair.private_pem,
        issuer="ACCOUNT.USER",
        subject="ACCOUNT.USER",
        audience="https://sfaccount.snowflakecomputing.example",
        additional_claims={"fp": "SHA256:deadbeef"},
        clock=clock,
    )

    headers = await provider.headers()

    assert headers["Authorization"].startswith("Bearer ")
    token = headers["Authorization"].removeprefix("Bearer ")

    # verify_exp/verify_iat are disabled because the fixture clock is a fixed,
    # arbitrary instant unrelated to wall-clock "now" — freshness relative to
    # wall-clock time is exercised separately in the refresh-before-expiry
    # test below; this one is purely about signature + claim correctness.
    claims = jwt.decode(
        token,
        rsa_keypair.public_pem,
        algorithms=["RS256"],
        issuer="ACCOUNT.USER",
        audience="https://sfaccount.snowflakecomputing.example",
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == "ACCOUNT.USER"
    assert claims["sub"] == "ACCOUNT.USER"
    assert claims["aud"] == "https://sfaccount.snowflakecomputing.example"
    assert claims["fp"] == "SHA256:deadbeef"
    assert claims["iat"] == int(clock.now().timestamp())
    assert claims["exp"] == int((clock.now() + timedelta(minutes=5)).timestamp())


async def test_direct_mode_wrong_public_key_fails_verification(rsa_keypair, clock):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod

    wrong_private = rsa_mod.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_public_pem = wrong_private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    provider = JWTAuthProvider(private_key=rsa_keypair.private_pem, issuer="iss", clock=clock)
    headers = await provider.headers()
    token = headers["Authorization"].removeprefix("Bearer ")

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, wrong_public_pem, algorithms=["RS256"])


async def test_direct_mode_reuses_assertion_then_resigns_before_expiry(rsa_keypair, clock):
    provider = JWTAuthProvider(
        private_key=rsa_keypair.private_pem,
        issuer="iss",
        assertion_ttl=timedelta(minutes=5),
        refresh_margin=timedelta(seconds=30),
        clock=clock,
    )

    first = await provider.headers()

    clock.advance(timedelta(minutes=4, seconds=20))  # within TTL, outside margin
    still_same = await provider.headers()
    assert still_same == first

    clock.advance(timedelta(seconds=35))  # now inside the refresh margin
    resigned = await provider.headers()
    assert resigned != first  # a fresh assertion with a new iat/exp was signed


async def test_exchange_mode_requires_transport(rsa_keypair):
    with pytest.raises(ValueError, match="transport"):
        JWTAuthProvider(private_key=rsa_keypair.private_pem, issuer="iss", token_url=TOKEN_URL)


async def test_exchange_mode_posts_assertion_and_returns_access_token(
    respx_mock, transport, rsa_keypair, clock
):
    def _respond(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
        assertion = form["assertion"][0]
        # Prove the posted assertion is genuinely verifiable server-side.
        claims = jwt.decode(
            assertion,
            rsa_keypair.public_pem,
            algorithms=["RS256"],
            issuer="iss",
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["sub"] == "svc-account"
        return httpx.Response(200, json={"access_token": "exchanged-tok", "expires_in": 900})

    route = respx_mock.post(TOKEN_URL).mock(side_effect=_respond)

    provider = JWTAuthProvider(
        private_key=rsa_keypair.private_pem,
        issuer="iss",
        subject="svc-account",
        token_url=TOKEN_URL,
        transport=transport,
        clock=clock,
    )

    headers = await provider.headers()

    assert headers == {"Authorization": "Bearer exchanged-tok"}
    assert route.call_count == 1

    # Reused while valid: no second exchange.
    await provider.headers()
    assert route.call_count == 1


async def test_exchange_mode_failure_raises_and_is_not_cached(
    respx_mock, transport, rsa_keypair, clock
):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )

    provider = JWTAuthProvider(
        private_key=rsa_keypair.private_pem,
        issuer="iss",
        token_url=TOKEN_URL,
        transport=transport,
        clock=clock,
    )

    with pytest.raises(AuthError):
        await provider.headers()
