"""``Connector.list_changed()`` is wired to the change feed -- T6.3's one authorized change
to ``__init__.py``.

The unit tests in this directory drive
:func:`~qlabs_connector_snowflake.read.list_changed_candidates` directly, with the endpoint
and tenant identity supplied by the test. That leaves exactly one thing unproven, and it is
the thing most likely to be wrong: whether the ``Connector`` passes the *right* identity
through. ``tenant_id`` in particular is not read off any row -- ``ACCOUNT_USAGE`` carries no
account column -- so it comes from ``SnowflakeConfig.account_identifier``, and it must be
the same value the refs handed to :meth:`Connector.read` carry, or the engine's IdentityMap
would split one physical object across two identities.

So this file runs the real connector: real key-pair JWT auth, real config, real
``HttpEndpoint``, over the same ``respx``-mocked statement endpoint, and then takes a
``ChangeRef`` the poll produced straight into :meth:`Connector.read` -- the exact sequence
the engine performs.
"""

from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from qlabs_catalog_sync_sdk.config import ConnectorContext, ManualClock
from qlabs_catalog_sync_sdk.contract import ChangeKind, EntityType, Watermark, WatermarkKind
from qlabs_catalog_sync_sdk.models import Dataset
from qlabs_connector_snowflake import Connector
from qlabs_connector_snowflake.auth import SnowflakeConfig

from ..conftest import (
    COLUMNS_COLUMNS,
    ENDPOINT,
    TABLES_COLUMNS,
    TAG_REFERENCE_COLUMNS,
    StatementRouter,
    column_row,
    statements_url,
    tag_reference_row,
)
from ..conftest import table_row as information_schema_table_row
from .conftest import NOW_1, set_now, set_tables, table_row


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


async def _connector() -> Connector:
    connector = Connector(clock=ManualClock())
    await connector.setup(
        ConnectorContext.build(
            config=SnowflakeConfig(
                organization="acme",
                account="primary",
                user="svc_qlabs",
                private_key=_private_key_pem(),
            ),
            endpoint=ENDPOINT,
            clock=ManualClock(),
        )
    )
    return connector


async def test_the_connector_polls_and_then_reads_the_ref_it_produced(
    respx_mock: Any, router: StatementRouter
) -> None:
    set_now(router, NOW_1)
    set_tables(router, [table_row("ORDERS", table_id="4242")])
    router.rows("INFORMATION_SCHEMA.TABLES", TABLES_COLUMNS, [information_schema_table_row()])
    router.rows("INFORMATION_SCHEMA.COLUMNS", COLUMNS_COLUMNS, [column_row()])
    router.rows("TAG_REFERENCES", TAG_REFERENCE_COLUMNS, [tag_reference_row()])
    respx_mock.post(statements_url()).mock(side_effect=router)
    connector = await _connector()

    result = await connector.list_changed(
        EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET)
    )

    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.kind is ChangeKind.UPSERT
    assert change.ref.native_key == "SALES_DB.PUBLIC.ORDERS"
    # The account identifier, upper-cased -- the same tenant id read() stamps on entities.
    assert change.ref.tenant_id == "ACME-PRIMARY"
    assert result.next_watermark.kind is WatermarkKind.CURSOR
    assert result.next_watermark.stream_key == f"{ENDPOINT}:dataset"

    entity = await connector.read(change.ref)

    assert isinstance(entity, Dataset)
    assert entity.identities[0].tenant_id == change.ref.tenant_id
    await connector.close()


async def test_the_connector_carries_its_role_and_warehouse_into_every_scan(
    respx_mock: Any, router: StatementRouter
) -> None:
    """The change feed builds its own ``StatementClient`` per call, so the config's role
    and warehouse have to be threaded through -- a scan running as the wrong role would
    silently see a different subset of the account."""
    set_now(router, NOW_1)
    set_tables(router, [])
    respx_mock.post(statements_url()).mock(side_effect=router)
    connector = Connector(clock=ManualClock())
    await connector.setup(
        ConnectorContext.build(
            config=SnowflakeConfig(
                organization="acme",
                account="primary",
                user="svc_qlabs",
                private_key=_private_key_pem(),
                role="QLABS_SYNC",
                warehouse="QLABS_WH",
            ),
            endpoint=ENDPOINT,
            clock=ManualClock(),
        )
    )

    await connector.list_changed(
        EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET)
    )

    body = router.body_for("ACCOUNT_USAGE.TABLES")
    assert body["role"] == "QLABS_SYNC"
    assert body["warehouse"] == "QLABS_WH"
    await connector.close()
