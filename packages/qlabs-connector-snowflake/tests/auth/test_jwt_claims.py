"""build_key_pair_jwt_provider: JWT claim construction (iss/sub/iat/exp), the
public-key fingerprint format, and the one-hour lifetime cap (RS-05 section 3.2).

Runs entirely against a locally generated throwaway RSA key — there is no live
Snowflake tenant to verify the exact server-side JWT rejection semantics against; see
this task's report for what stays TENANT-UNVERIFIED.
"""

from __future__ import annotations

from datetime import timedelta

import jwt
import pytest

from qlabs_connector_snowflake.auth import (
    DEFAULT_JWT_LIFETIME,
    SNOWFLAKE_JWT_MAX_LIFETIME,
    build_key_pair_jwt_provider,
    compute_public_key_fingerprint,
)

from .conftest import build_config


def _decode_unverified(token: str) -> dict[str, object]:
    # Only this test module needs claim inspection without a peer to verify against;
    # `verify_signature=False` is safe here because the point of these assertions is
    # the claim *shape*, and signature validity is exercised separately by simply
    # decoding successfully with the matching public key below.
    return jwt.decode(token, options={"verify_signature": False})


async def test_fingerprint_has_the_sha256_prefixed_base64_shape(rsa_keypair) -> None:
    fingerprint = compute_public_key_fingerprint(rsa_keypair.private_key)

    assert fingerprint.startswith("SHA256:")
    # Header (7 chars) + 44 base64 chars (32-byte SHA-256 digest, base64-encoded).
    assert len(fingerprint) == len("SHA256:") + 44


async def test_fingerprint_is_deterministic_for_the_same_key(rsa_keypair) -> None:
    first = compute_public_key_fingerprint(rsa_keypair.private_key)
    second = compute_public_key_fingerprint(rsa_keypair.private_key)

    assert first == second


async def test_fingerprint_differs_for_different_keys(rsa_keypair, clock) -> None:
    other_config = build_config()
    other_provider = build_key_pair_jwt_provider(other_config, endpoint="snowflake", clock=clock)
    other_headers = await other_provider.headers()
    other_claims = _decode_unverified(other_headers["Authorization"].removeprefix("Bearer "))
    other_fingerprint = str(other_claims["iss"]).rsplit(".", 1)[-1]

    own_fingerprint = compute_public_key_fingerprint(rsa_keypair.private_key)

    assert other_fingerprint != own_fingerprint


async def test_issuer_and_subject_claims_match_rs05_shape(rsa_keypair, clock) -> None:
    config = build_config(
        organization="acme",
        account="primary",
        user="svc_qlabs",
        private_key=rsa_keypair.private_pem,
    )
    fingerprint = compute_public_key_fingerprint(rsa_keypair.private_key)

    provider = build_key_pair_jwt_provider(config, endpoint="snowflake", clock=clock)
    headers = await provider.headers()
    token = headers["Authorization"].removeprefix("Bearer ")
    claims = _decode_unverified(token)

    assert claims["iss"] == f"ACME-PRIMARY.SVC_QLABS.{fingerprint}"
    assert claims["sub"] == "ACME-PRIMARY.SVC_QLABS"


async def test_token_verifies_against_the_matching_public_key(rsa_keypair, clock) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)
    provider = build_key_pair_jwt_provider(config, endpoint="snowflake", clock=clock)

    headers = await provider.headers()
    token = headers["Authorization"].removeprefix("Bearer ")

    # verify_exp/verify_iat are disabled because the fixture clock is a fixed, arbitrary
    # instant unrelated to wall-clock "now" (matching the SDK's own test_jwt.py pattern
    # for the same reason) — this assertion is purely about signature + claim
    # correctness, not freshness relative to real time.
    claims = jwt.decode(
        token,
        rsa_keypair.public_pem,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["sub"] == config.account_identifier + "." + config.user_identifier


async def test_iat_and_exp_reflect_the_injected_clock_and_requested_lifetime(
    rsa_keypair, clock
) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)
    lifetime = timedelta(minutes=15)

    provider = build_key_pair_jwt_provider(
        config, endpoint="snowflake", lifetime=lifetime, clock=clock
    )
    headers = await provider.headers()
    claims = _decode_unverified(headers["Authorization"].removeprefix("Bearer "))

    assert claims["iat"] == int(clock.now().timestamp())
    assert claims["exp"] == int((clock.now() + lifetime).timestamp())


async def test_default_lifetime_is_under_the_one_hour_cap() -> None:
    assert DEFAULT_JWT_LIFETIME < SNOWFLAKE_JWT_MAX_LIFETIME
    assert timedelta(minutes=59) == DEFAULT_JWT_LIFETIME


def test_lifetime_at_exactly_the_cap_is_accepted(rsa_keypair, clock) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)

    build_key_pair_jwt_provider(
        config, endpoint="snowflake", lifetime=SNOWFLAKE_JWT_MAX_LIFETIME, clock=clock
    )  # must not raise


def test_lifetime_above_the_one_hour_cap_is_rejected(rsa_keypair, clock) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)

    with pytest.raises(ValueError, match="one hour"):
        build_key_pair_jwt_provider(
            config,
            endpoint="snowflake",
            lifetime=SNOWFLAKE_JWT_MAX_LIFETIME + timedelta(seconds=1),
            clock=clock,
        )


def test_lifetime_cap_is_checked_before_the_private_key_is_touched(clock) -> None:
    """A malformed key must not mask the lifetime-cap error: the cap check runs first."""
    config = build_config(
        private_key="-----BEGIN PRIVATE KEY-----\nnot-real\n-----END PRIVATE KEY-----"
    )

    with pytest.raises(ValueError, match="one hour"):
        build_key_pair_jwt_provider(
            config,
            endpoint="snowflake",
            lifetime=SNOWFLAKE_JWT_MAX_LIFETIME + timedelta(seconds=1),
            clock=clock,
        )
