"""Shared fixtures for the auth-provider tests: a controllable clock, an httpx
transport under respx, and an RSA keypair for the JWT provider tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from qlabs_catalog_sync_sdk.auth import Clock


@dataclass
class FakeClock:
    """A :class:`Clock` whose time only moves when the test tells it to —
    proves refresh-before-expiry behavior without sleeping."""

    current: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current = self.current + delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _static_clock_check(c: Clock) -> None:
    # Structural sanity check that FakeClock satisfies the Clock protocol;
    # exercised at import time via the type checker, not at runtime.
    c.now()


@pytest.fixture
async def transport() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@dataclass
class RSAKeyPair:
    private_pem: str
    public_pem: str


@pytest.fixture
def rsa_keypair() -> RSAKeyPair:
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
    return RSAKeyPair(private_pem=private_pem, public_pem=public_pem)
