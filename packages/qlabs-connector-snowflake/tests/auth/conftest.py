"""Shared fixtures for the Snowflake connector's auth/config/setup/healthcheck tests.

Mirrors the SDK's own ``tests/auth/conftest.py`` (a controllable clock, an RSA keypair
for the JWT provider tests) and the Databricks connector's ``tests/auth/conftest.py``
(a valid config factory, a bound ``ConnectorContext`` factory) — adapted for
Snowflake's key-pair JWT auth rather than Databricks' OAuth M2M. There is no live
Snowflake tenant for this build; every test here runs against a locally generated
throwaway RSA key and ``respx``-mocked HTTP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from qlabs_catalog_sync_sdk.config import ConnectorContext, ManualClock
from qlabs_connector_snowflake.auth import SnowflakeConfig

BASE_URL = "https://acme-primary.snowflakecomputing.com"
DATABASES_URL = f"{BASE_URL}/api/v2/databases"


@pytest.fixture
def clock() -> ManualClock:
    """The SDK's own controllable clock — time only moves when a test advances it."""
    return ManualClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
async def transport() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@dataclass
class RSAKeyPair:
    private_pem: str
    public_pem: str
    private_key: rsa.RSAPrivateKey


def _generate_rsa_keypair() -> RSAKeyPair:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return RSAKeyPair(private_pem=private_pem, public_pem=public_pem, private_key=private_key)


@pytest.fixture
def rsa_keypair() -> RSAKeyPair:
    return _generate_rsa_keypair()


@pytest.fixture
def encrypted_rsa_keypair() -> tuple[RSAKeyPair, bytes]:
    """A second keypair whose PEM is passphrase-encrypted, plus the passphrase."""
    passphrase = b"MARKER-KEY-PASSPHRASE-7c1d"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(passphrase),
    ).decode("ascii")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    keypair = RSAKeyPair(private_pem=private_pem, public_pem=public_pem, private_key=private_key)
    return keypair, passphrase


def build_config(**overrides: Any) -> SnowflakeConfig:
    """A minimally valid :class:`SnowflakeConfig`, with any field overridden."""
    values: dict[str, Any] = {
        "organization": "acme",
        "account": "primary",
        "user": "svc_qlabs",
        "private_key": _generate_rsa_keypair().private_pem,
    }
    values.update(overrides)
    return SnowflakeConfig(**values)


def build_ctx(config: SnowflakeConfig | None = None) -> ConnectorContext[SnowflakeConfig]:
    return ConnectorContext.build(
        config=config or build_config(), endpoint="snowflake", clock=ManualClock()
    )
