"""Failures come back as the SDK's typed exceptions, never as a quietly empty result.

An empty ``ListChangedResult`` means "nothing changed" and the engine acts on it, so any
failure that could hide behind one has to raise instead. Everything here goes through
:class:`StatementClient`, which routes every ``httpx`` failure through
:func:`~qlabs_connector_snowflake.auth.translate_snowflake_error` -- no second error
hierarchy is invented for the change feed (agent-guide convention).

The one deliberate exception is ``SHOW LISTINGS``. Listing visibility is a separate
privilege a read-only sync role often does not hold (RS-05 section 3.6), and the schema
shape shares the ``DATA_PRODUCT`` stream with listings, so a permission refusal there
degrades to "listings not scanned this cycle" rather than taking schemas down with it --
with the previous listing census preserved, so nothing is reported as deleted merely
because it became invisible. A *transient* failure on the same statement still propagates:
retrying that is meaningful, and swallowing it would turn a blip into silently missing
candidates on every cycle.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from qlabs_catalog_sync_sdk.contract import EntityType, Watermark
from qlabs_catalog_sync_sdk.exceptions import AuthError, CapabilityError, TransientError
from qlabs_connector_snowflake.read import StatementClient

from ..conftest import ENDPOINT, listing_row, statements_url
from .conftest import (
    LISTINGS_SQL,
    NOW_1,
    NOW_2,
    StatementRouter,
    cursor,
    kinds_by_key,
    poll,
    schema_row,
    set_listings,
    set_now,
    set_schemata,
    set_tables,
    table_row,
)


async def test_a_401_becomes_an_auth_error(
    client: StatementClient, respx_mock: Any, router: StatementRouter
) -> None:
    respx_mock.post(statements_url()).mock(
        return_value=httpx.Response(401, json={"message": "invalid JWT"})
    )

    with pytest.raises(AuthError):
        await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))


async def test_a_403_also_becomes_an_auth_error(
    client: StatementClient, respx_mock: Any, router: StatementRouter
) -> None:
    respx_mock.post(statements_url()).mock(
        return_value=httpx.Response(403, json={"message": "role lacks IMPORTED PRIVILEGES"})
    )

    with pytest.raises(AuthError):
        await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))


async def test_a_500_on_the_scan_becomes_a_transient_error(
    client: StatementClient, respx_mock: Any, router: StatementRouter
) -> None:
    respx_mock.post(statements_url()).mock(
        return_value=httpx.Response(503, json={"message": "cloud services unavailable"})
    )

    with pytest.raises(TransientError):
        await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))


async def test_a_bad_statement_becomes_a_capability_error(
    client: StatementClient, respx_mock: Any, router: StatementRouter
) -> None:
    """A 400 is Snowflake saying the statement itself was rejected -- an unknown column on
    a view this build could not verify against a tenant is exactly that shape, and it must
    surface as "this connector asked for something this account cannot answer", not as a
    transient blip that gets retried forever."""
    respx_mock.post(statements_url()).mock(
        return_value=httpx.Response(400, json={"message": "invalid identifier 'TABLE_ID'"})
    )

    with pytest.raises(CapabilityError):
        await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))


async def test_glossary_term_is_refused_with_a_capability_error(
    client: StatementClient,
) -> None:
    with pytest.raises(CapabilityError):
        await poll(
            client,
            EntityType.GLOSSARY_TERM,
            Watermark.initial(ENDPOINT, EntityType.GLOSSARY_TERM),
        )


async def test_category_is_refused_with_a_capability_error(client: StatementClient) -> None:
    with pytest.raises(CapabilityError):
        await poll(client, EntityType.CATEGORY, Watermark.initial(ENDPOINT, EntityType.CATEGORY))


async def test_an_unsupported_entity_type_is_refused_before_any_statement_runs(
    client: StatementClient, router: StatementRouter
) -> None:
    with pytest.raises(CapabilityError):
        await poll(
            client,
            EntityType.GLOSSARY_TERM,
            Watermark.initial(ENDPOINT, EntityType.GLOSSARY_TERM),
        )

    assert router.statements == []


async def test_a_listing_permission_refusal_does_not_take_schemas_down_with_it(
    client: StatementClient, router: StatementRouter
) -> None:
    set_now(router, NOW_1)
    set_schemata(router, [schema_row("PUBLIC", schema_id="10")])
    router.on(LISTINGS_SQL, httpx.Response(403, json={"message": "insufficient privileges"}))

    result = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )

    assert kinds_by_key(result) == {"SALES_DB.PUBLIC": "upsert"}
    assert cursor(result)["listings"] == {}


async def test_losing_listing_visibility_does_not_orphan_the_listings_already_known(
    client: StatementClient, router: StatementRouter
) -> None:
    """The failure mode this degradation exists to prevent: a role that loses listing
    privileges mid-life must not make every listing look deleted. The census is carried
    forward untouched, so nothing is reported and nothing is forgotten."""
    set_now(router, NOW_1, NOW_2)
    set_schemata(router, [], [])
    router.on(
        LISTINGS_SQL,
        httpx.Response(
            200,
            json={
                "resultSetMetaData": {
                    "rowType": [{"name": name} for name in ("name", "global_name")]
                },
                "data": [["SALES_DAILY", "GZTS1"]],
            },
        ),
        httpx.Response(403, json={"message": "insufficient privileges"}),
    )

    first = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )
    assert set(cursor(first)["listings"]) == {"GZTS1"}

    second = await poll(client, EntityType.DATA_PRODUCT, first.next_watermark)

    assert second.is_empty
    assert set(cursor(second)["listings"]) == {"GZTS1"}


async def test_a_transient_listing_failure_still_propagates(
    client: StatementClient, router: StatementRouter
) -> None:
    """Degrading on a permission refusal is a considered exception; degrading on anything
    retryable would turn an outage into silently missing candidates."""
    set_now(router, NOW_1)
    set_schemata(router, [])
    router.on(LISTINGS_SQL, httpx.Response(503, json={"message": "try again"}))

    with pytest.raises(TransientError):
        await poll(
            client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
        )


async def test_a_row_with_no_usable_identity_is_skipped_not_crashed_on(
    client: StatementClient, router: StatementRouter
) -> None:
    """The projections are TENANT-UNVERIFIED. A row missing a name column cannot become an
    identity, and one unusable row must not cost the whole scan."""
    set_now(router, NOW_1)
    set_tables(
        router,
        [table_row("ORDERS", table_id="1"), table_row(None, table_id="2")],  # type: ignore[arg-type]
    )

    result = await poll(client, EntityType.DATASET, Watermark.initial(ENDPOINT, EntityType.DATASET))

    assert kinds_by_key(result) == {"SALES_DB.PUBLIC.ORDERS": "upsert"}


async def test_a_listing_without_a_global_name_is_skipped(
    client: StatementClient, router: StatementRouter
) -> None:
    """``global_name`` is the listing's identity (RS-05 section 2.4). A row without one has
    no key to sync under, and inventing one from the local name would collide across
    regions."""
    set_now(router, NOW_1)
    set_schemata(router, [])
    set_listings(
        router,
        [listing_row(name="OK", global_name="GZTS1"), listing_row(name="BROKEN", global_name="")],
    )

    result = await poll(
        client, EntityType.DATA_PRODUCT, Watermark.initial(ENDPOINT, EntityType.DATA_PRODUCT)
    )

    assert kinds_by_key(result) == {"GZTS1": "upsert"}
