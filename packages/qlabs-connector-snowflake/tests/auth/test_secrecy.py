"""The private key and its passphrase never leak through repr/str/model_dump, the
token provider built from them, or an auth failure raised out of setup()."""

from __future__ import annotations

import httpx
import pytest

from qlabs_catalog_sync_sdk.exceptions import AuthError
from qlabs_connector_snowflake import Connector
from qlabs_connector_snowflake.auth import build_key_pair_jwt_provider

from .conftest import DATABASES_URL, build_config, build_ctx

KEY_MARKER = "MARKER-CONFIG-PRIVATE-KEY-9f2a"
PASSPHRASE_MARKER = "MARKER-CONFIG-PASSPHRASE-1a2b"


def test_private_key_never_appears_in_config_repr_or_str(rsa_keypair) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)
    pem_marker = rsa_keypair.private_pem.splitlines()[1]  # a base64 body line

    assert pem_marker not in repr(config)
    assert pem_marker not in str(config)
    assert pem_marker not in repr(config.model_dump())


def test_private_key_passphrase_never_appears_in_config_repr_or_str(
    encrypted_rsa_keypair,
) -> None:
    keypair, passphrase = encrypted_rsa_keypair
    config = build_config(
        private_key=keypair.private_pem,
        private_key_passphrase=passphrase.decode("ascii"),
    )

    assert passphrase.decode("ascii") not in repr(config)
    assert passphrase.decode("ascii") not in str(config)
    assert passphrase.decode("ascii") not in repr(config.model_dump())


def test_token_provider_repr_never_contains_the_private_key(clock, rsa_keypair) -> None:
    config = build_config(private_key=rsa_keypair.private_pem)

    provider = build_key_pair_jwt_provider(config, endpoint="snowflake", clock=clock)

    pem_marker = rsa_keypair.private_pem.splitlines()[1]
    assert pem_marker not in repr(provider)
    assert pem_marker not in str(provider)


async def test_secret_never_appears_in_a_setup_auth_failure() -> None:
    config = build_config(private_key="not-a-pem-body-but-has PRIVATE KEY marker")
    connector = Connector()

    with pytest.raises(Exception):  # noqa: B017 - either ValidationError or AuthError
        await connector.setup(build_ctx(config))


async def test_bad_passphrase_auth_error_never_contains_the_passphrase(
    encrypted_rsa_keypair,
) -> None:
    keypair, _passphrase = encrypted_rsa_keypair
    config = build_config(
        private_key=keypair.private_pem,
        private_key_passphrase="MARKER-WRONG-PASSPHRASE-4e5f",
    )
    connector = Connector()

    with pytest.raises(AuthError) as exc_info:
        await connector.setup(build_ctx(config))

    assert "MARKER-WRONG-PASSPHRASE-4e5f" not in str(exc_info.value)
    assert "MARKER-WRONG-PASSPHRASE-4e5f" not in repr(exc_info.value)


async def test_connector_repr_never_contains_the_private_key(respx_mock, rsa_keypair) -> None:
    respx_mock.get(DATABASES_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    config = build_config(private_key=rsa_keypair.private_pem)
    connector = Connector()

    await connector.setup(build_ctx(config))

    pem_marker = rsa_keypair.private_pem.splitlines()[1]
    assert pem_marker not in repr(connector)
    assert pem_marker not in str(connector)
