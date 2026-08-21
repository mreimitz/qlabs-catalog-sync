"""``Connector.read()`` is wired to the read path (T6.4's one authorized change to
``__init__.py``): the real connector, set up with real key-pair JWT auth, reading a real
neutral entity over ``respx``-mocked HTTP.

``list_changed`` is wired too (T6.3's one authorized change to the same file); its own
behavior lives in ``tests/read/changes/``. The write path still refuses through the
inherited defaults.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from qlabs_catalog_sync_sdk.config import ConnectorContext, ManualClock
from qlabs_catalog_sync_sdk.contract import Watermark
from qlabs_catalog_sync_sdk.exceptions import CapabilityError, NotFound
from qlabs_catalog_sync_sdk.models import AssetType, Dataset, EntityType
from qlabs_connector_snowflake import Connector
from qlabs_connector_snowflake.auth import SnowflakeConfig

from .conftest import (
    COLUMNS_COLUMNS,
    ENDPOINT,
    TABLES_COLUMNS,
    TAG_REFERENCE_COLUMNS,
    StatementRouter,
    column_row,
    dataset_ref,
    statements_url,
    table_row,
    tag_reference_row,
)


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


async def _connector(**config_overrides: object) -> Connector:
    values: dict[str, object] = {
        "organization": "acme",
        "account": "primary",
        "user": "svc_qlabs",
        "private_key": _private_key_pem(),
    }
    values.update(config_overrides)
    connector = Connector(clock=ManualClock())
    await connector.setup(
        ConnectorContext.build(
            config=SnowflakeConfig(**values), endpoint=ENDPOINT, clock=ManualClock()
        )
    )
    return connector


async def test_the_connector_reads_a_dataset_end_to_end(
    respx_mock: object, router: StatementRouter
) -> None:
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row()])
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [column_row()])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [tag_reference_row()])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]
    connector = await _connector()

    entity = await connector.read(dataset_ref())

    assert isinstance(entity, Dataset)
    assert entity.name == "ORDERS"
    assert entity.asset_type is AssetType.TABLE
    assert entity.field_envelopes["name"].source_endpoint == ENDPOINT
    await connector.close()


async def test_the_configured_role_and_warehouse_reach_the_statement(
    respx_mock: object, router: StatementRouter
) -> None:
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row()])
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]
    connector = await _connector(role="QLABS_SYNC", warehouse="QLABS_WH")

    await connector.read(dataset_ref())

    body = router.body_for("INFORMATION_SCHEMA.TABLES")
    assert body["role"] == "QLABS_SYNC"
    assert body["warehouse"] == "QLABS_WH"
    await connector.close()


async def test_the_request_carries_the_key_pair_jwt_headers(
    respx_mock: object, router: StatementRouter
) -> None:
    """The auth wiring T6.1 built is still what carries these reads."""
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [table_row()])
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]
    connector = await _connector()

    await connector.read(dataset_ref())

    request = router.requests[0]
    assert request.headers["Authorization"].startswith("Bearer ")
    assert request.headers["X-Snowflake-Authorization-Token-Type"] == "KEYPAIR_JWT"
    await connector.close()


async def test_a_missing_object_surfaces_as_the_sdk_not_found(
    respx_mock: object, router: StatementRouter
) -> None:
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [])
    router.rows("INFORMATION_SCHEMA.VIEWS", TABLES_COLUMNS, [])
    respx_mock.post(statements_url()).mock(side_effect=router)  # type: ignore[attr-defined]
    connector = await _connector()

    with pytest.raises(NotFound):
        await connector.read(dataset_ref("SALES_DB.PUBLIC.GONE"))
    await connector.close()


async def test_read_before_setup_is_a_programming_error_not_a_silent_empty() -> None:
    with pytest.raises(RuntimeError, match="setup"):
        await Connector().read(dataset_ref())


async def test_the_write_path_still_refuses() -> None:
    """A read-only source connector stays read-only: T6.4 added a read, nothing else."""
    connector = await _connector()

    with pytest.raises(CapabilityError):
        await connector.delete(dataset_ref())
    await connector.close()


async def test_list_changed_before_setup_is_a_programming_error_not_a_silent_empty() -> None:
    """The read path's lifecycle guard, mirrored on the change feed: a connector that was
    never set up must say so rather than answer "nothing changed"."""
    with pytest.raises(RuntimeError, match="setup"):
        await Connector().list_changed(
            EntityType.DATA_PRODUCT,
            Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT),
        )
